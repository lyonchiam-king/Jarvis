"""Vertex's image endpoint -- the IMAGE half of demo-site generation.

Outbound egress is blocked in this container, so nothing here reaches
Vertex. Every test drives ``VertexImageClient`` through an injected fake
session built from Google's documented error-detail envelope (the same
``google.rpc`` QuotaFailure/RetryInfo shape ``sitegen`` already parses) and
the ``generateContent`` reply shape described in Vertex's docs -- none of it
is checked against a live endpoint.
"""

import pytest

from outreach.errors import is_retryable
from outreach.providers.sitegen import SiteGenError
from outreach.providers.vertex_image import VertexImageClient

VALID_PNG = b"\x89PNG\r\n\x1a\ntinyimagebytes"
VALID_B64 = __import__("base64").b64encode(VALID_PNG).decode()


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


def image_response(mime="image/png", finish_reason="STOP", **extra):
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"inlineData": {"mimeType": mime, "data": VALID_B64}}
                    ]
                },
                "finishReason": finish_reason,
                **extra,
            }
        ]
    }
    return FakeResponse(payload)


def text_only_response(text="sorry, I can't draw that", finish_reason="STOP"):
    payload = {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "finishReason": finish_reason,
            }
        ]
    }
    return FakeResponse(payload)


class FakeSession:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.posts = []
        self.gets = []

    def post(self, url, json, headers, timeout):
        self.posts.append({"url": url, "body": json, "headers": headers, "timeout": timeout})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def get(self, url, headers, params, timeout):
        self.gets.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def client(*replies, token="tok", sleep=None, **kwargs):
    return VertexImageClient(
        lambda: token,
        project=kwargs.pop("project", "my-project"),
        region=kwargs.pop("region", "global"),
        model=kwargs.pop("model", "imagen-4"),
        session=FakeSession(*replies),
        sleep=sleep if sleep is not None else (lambda s: None),
        **kwargs,
    )


# --------------------------------------------------------------- URL shape


def test_the_global_url_has_no_region_segment_duplicated():
    c = client(image_response(), project="proj-1", region="global", model="imagen-4")
    c.generate_image("a cosy bakery")

    post = c._session.posts[0]
    assert post["url"] == (
        "https://aiplatform.googleapis.com/v1beta1/projects/proj-1/locations/"
        "global/publishers/google/models/imagen-4:generateContent"
    )
    assert c.endpoint == post["url"]


def test_a_named_region_uses_the_regional_host_and_path():
    c = client(image_response(), project="proj-1", region="us-central1", model="imagen-4")
    c.generate_image("a cosy bakery")

    post = c._session.posts[0]
    assert post["url"] == (
        "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/proj-1/"
        "locations/us-central1/publishers/google/models/imagen-4:"
        "generateContent"
    )


def test_a_full_publishers_path_is_stripped_down_to_the_bare_model_name():
    c = client(
        image_response(),
        model="publishers/google/models/imagen-4",
        project="proj-1",
    )
    assert c.model == "imagen-4"
    c.generate_image("prompt")
    assert "publishers/google/models/imagen-4:generateContent" in c._session.posts[0]["url"]
    # not doubled up
    assert "models/publishers" not in c._session.posts[0]["url"]


# --------------------------------------------------------------- headers


def test_the_quota_project_header_is_always_sent():
    c = client(image_response(), project="proj-1", token="my-bearer-token")
    c.generate_image("prompt")

    headers = c._session.posts[0]["headers"]
    assert headers["Authorization"] == "Bearer my-bearer-token"
    assert headers["x-goog-user-project"] == "proj-1"
    assert headers["Content-Type"] == "application/json"


def test_the_token_provider_is_called_fresh_each_time():
    """A captured string would go stale on a run that outlives one hour;
    a callable re-fetches (and, upstream, refreshes) on every call."""
    calls = []

    def provider():
        calls.append(1)
        return f"token-{len(calls)}"

    c = VertexImageClient(
        provider,
        project="proj-1",
        region="global",
        model="imagen-4",
        session=FakeSession(image_response(), image_response()),
        sleep=lambda s: None,
    )
    c.generate_image("a")
    c.generate_image("b")

    assert [p["headers"]["Authorization"] for p in c._session.posts] == [
        "Bearer token-1",
        "Bearer token-2",
    ]


# --------------------------------------------------------------- body shape


def test_image_size_is_omitted_from_the_body_when_blank():
    c = client(image_response(), image_size="")
    c.generate_image("prompt")

    body = c._session.posts[0]["body"]
    assert "imageConfig" not in body["generationConfig"]
    assert body["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    assert body["contents"] == [{"role": "user", "parts": [{"text": "prompt"}]}]


def test_image_size_is_sent_only_when_set():
    c = client(image_response(), image_size="1K")
    c.generate_image("prompt")

    body = c._session.posts[0]["body"]
    assert body["generationConfig"]["imageConfig"] == {"imageSize": "1K"}


# --------------------------------------------------------------- happy path


def test_generate_image_returns_bytes_and_mime():
    c = client(image_response(mime="image/png"))
    data, mime = c.generate_image("prompt")

    assert data == VALID_PNG
    assert mime == "image/png"


def test_call_count_increments_per_call():
    c = client(image_response(), image_response())
    c.generate_image("a")
    c.generate_image("b")
    assert c.call_count == 2


# --------------------------------------------------------------- refusals


def test_a_text_only_reply_points_at_vertex_image_model():
    c = client(text_only_response(), model="imagen-4")

    with pytest.raises(SiteGenError, match="build.vertex_image_model"):
        c.generate_image("prompt")


def test_safety_finish_reason_names_the_category():
    reply = image_response(
        finish_reason="SAFETY",
        safetyRatings=[
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "blocked": False},
            {"category": "HARM_CATEGORY_HARASSMENT", "blocked": True},
        ],
    )
    c = client(reply)

    with pytest.raises(SiteGenError, match="HARM_CATEGORY_HARASSMENT"):
        c.generate_image("prompt")


def test_prohibited_content_finish_reason_also_raises():
    reply = image_response(finish_reason="PROHIBITED_CONTENT")
    c = client(reply)

    with pytest.raises(SiteGenError, match="PROHIBITED_CONTENT"):
        c.generate_image("prompt")


def test_prompt_feedback_block_reason_raises():
    payload = {"candidates": [], "promptFeedback": {"blockReason": "OTHER"}}
    c = client(FakeResponse(payload))

    with pytest.raises(SiteGenError, match="blocked the prompt") as exc_info:
        c.generate_image("prompt")
    assert "OTHER" in str(exc_info.value)


# --------------------------------------------------------------- status mapping


def test_service_disabled_403_names_the_console_path():
    body = {
        "error": {
            "code": 403,
            "message": (
                "Vertex AI API has not been used in project proj-1 before or "
                "it is disabled"
            ),
            "status": "PERMISSION_DENIED",
        }
    }
    c = client(
        FakeResponse(body, status_code=403, text=body["error"]["message"]),
        max_attempts=1,
    )

    with pytest.raises(SiteGenError, match="not switched on") as exc_info:
        c.generate_image("prompt")
    assert exc_info.value.status_code == 403
    assert not is_retryable(exc_info.value)


def test_service_disabled_by_the_other_phrase_is_also_recognised():
    c = client(
        FakeResponse(status_code=403, text="SERVICE_DISABLED: aiplatform.googleapis.com"),
        max_attempts=1,
    )
    with pytest.raises(SiteGenError, match="not switched on"):
        c.generate_image("prompt")


def test_billing_403_names_billing():
    # The match string is deliberately something only our own message would
    # say -- Google's raw text ("requires billing to be enabled") also
    # contains the word "billing" and gets echoed into the generic HTTP-403
    # fallback too, so asserting on that word alone would pass even if this
    # branch were deleted.
    c = client(
        FakeResponse(status_code=403, text="This API method requires billing to be enabled"),
        max_attempts=1,
    )
    with pytest.raises(SiteGenError, match="link a billing account"):
        c.generate_image("prompt")


def test_401_points_at_the_vertex_token_file_and_reassures_about_sheets():
    c = client(FakeResponse(status_code=401, text="unauthorized"), max_attempts=1)

    with pytest.raises(SiteGenError) as exc_info:
        c.generate_image("prompt")
    message = str(exc_info.value)
    assert "vertex_token.json" in message
    assert "Sheets and Gmail" in message
    assert not is_retryable(exc_info.value)


def test_403_mentioning_scope_also_points_at_the_vertex_token_file():
    c = client(
        FakeResponse(status_code=403, text="Request had insufficient authentication scopes"),
        max_attempts=1,
    )
    with pytest.raises(SiteGenError, match="vertex_token.json"):
        c.generate_image("prompt")


def test_404_names_the_model_and_region_and_points_at_vertex_models():
    c = client(
        FakeResponse(status_code=404, text="not found"),
        model="imagen-9-nope",
        region="us-central1",
        max_attempts=1,
    )
    with pytest.raises(SiteGenError) as exc_info:
        c.generate_image("prompt")
    message = str(exc_info.value)
    assert "imagen-9-nope" in message
    assert "us-central1" in message
    assert "vertex-models" in message
    assert not is_retryable(exc_info.value)


def test_a_busy_model_is_retried_with_backoff():
    slept = []
    c = client(
        FakeResponse(status_code=503, text="high demand"),
        FakeResponse(status_code=503, text="high demand"),
        image_response(),
        sleep=slept.append,
    )

    data, mime = c.generate_image("prompt")

    assert data == VALID_PNG
    assert c.call_count == 3
    assert slept and slept[0] < slept[1]


def test_giving_up_after_the_last_attempt_is_still_retryable():
    c = client(*[FakeResponse(status_code=503, text="high demand")] * 3)

    with pytest.raises(SiteGenError) as exc_info:
        c.generate_image("prompt")
    assert is_retryable(exc_info.value)


def test_a_quota_error_carries_the_quota_name_and_retry_delay_through():
    body = {
        "error": {
            "code": 429,
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": "GenerateContentPerMinutePerProject"}],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "13s",
                },
            ],
        }
    }
    c = client(FakeResponse(body, status_code=429, text="quota"), max_attempts=1)

    with pytest.raises(SiteGenError) as exc_info:
        c.generate_image("prompt")

    assert exc_info.value.quota == "GenerateContentPerMinutePerProject"
    assert exc_info.value.retry_after == 13.0
    assert is_retryable(exc_info.value)


def test_a_400_is_never_retried():
    c = client(*[FakeResponse(status_code=400, text="bad request")] * 3)

    with pytest.raises(SiteGenError) as exc_info:
        c.generate_image("prompt")
    assert c.call_count == 1
    assert not is_retryable(exc_info.value)


def test_an_unparseable_200_body_raises():
    c = client(FakeResponse(not_json=True))
    with pytest.raises(SiteGenError, match="not JSON"):
        c.generate_image("prompt")


# --------------------------------------------------------------- list_models


def test_list_models_hits_the_publisher_scoped_endpoint():
    session = FakeSession(FakeResponse({"publisherModels": []}))
    c = VertexImageClient(
        lambda: "tok", project="proj-1", region="global", model="imagen-4",
        session=session, sleep=lambda s: None,
    )
    c.list_models()

    get = session.gets[0]
    assert get["url"] == "https://aiplatform.googleapis.com/v1beta1/publishers/google/models"
    assert get["params"] == {"parent": "publishers/google"}
    assert get["headers"]["Authorization"] == "Bearer tok"
    assert get["headers"]["x-goog-user-project"] == "proj-1"


def test_list_models_strips_the_publishers_google_models_prefix():
    payload = {
        "publisherModels": [
            {"name": "publishers/google/models/imagen-4"},
            {"name": "publishers/google/models/imagen-3-fast"},
        ]
    }
    session = FakeSession(FakeResponse(payload))
    c = VertexImageClient(
        lambda: "tok", project="proj-1", region="global", model="imagen-4",
        session=session, sleep=lambda s: None,
    )

    assert c.list_models() == ["imagen-4", "imagen-3-fast"]


def test_list_models_reports_a_non_200_without_pretending_it_found_nothing():
    # 400 rather than 500: a 5xx is already caught (and turned into a
    # SiteGenError) by the shared 403/429/5xx mapping before list_models's
    # own "did this come back 200" check ever runs, so it would not exercise
    # that check at all.
    session = FakeSession(FakeResponse(status_code=400, text="bad request"))
    c = VertexImageClient(
        lambda: "tok", project="proj-1", region="global", model="imagen-4",
        session=session, sleep=lambda s: None,
    )
    with pytest.raises(SiteGenError, match="Could not list Vertex models"):
        c.list_models()


def test_list_models_service_disabled_reuses_the_same_message():
    session = FakeSession(
        FakeResponse(status_code=403, text="SERVICE_DISABLED: aiplatform.googleapis.com")
    )
    c = VertexImageClient(
        lambda: "tok", project="proj-1", region="global", model="imagen-4",
        session=session, sleep=lambda s: None,
    )
    with pytest.raises(SiteGenError, match="not switched on"):
        c.list_models()


def test_the_api_version_defaults_to_the_one_the_catalogue_uses():
    """The publisher catalogue lists Gemini 3.x image models under v1beta1.
    Asking v1 for one returns 404 'no such model', which reads as a wrong
    name and sends you hunting through regions instead -- which is exactly
    what happened."""
    from outreach.providers.vertex_image import VertexImageClient

    client = VertexImageClient(
        lambda: "t", project="p", region="global", model="gemini-3.1-flash-image"
    )

    assert "/v1beta1/" in client.endpoint
    assert "/v1/" not in client.endpoint


def test_the_api_version_can_be_overridden():
    from outreach.providers.vertex_image import VertexImageClient

    client = VertexImageClient(
        lambda: "t", project="p", region="global",
        model="m", api_version="v1",
    )

    assert "/v1/projects/" in client.endpoint
