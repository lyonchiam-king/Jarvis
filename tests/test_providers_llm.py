"""Switching the model provider without changing what the pipeline gets back.

Anthropic enforces the schema; Ollama and OpenRouter may not. These tests pin
the difference: a reply that ignores the schema must never reach the Sheet.
"""

import json as _json
from dataclasses import replace

import pytest

from outreach.config import load_config
from outreach.providers.llm import (
    BRIEF_SCHEMA,
    LLMError,
    OpenAICompatibleWriter,
    make_writer,
)

PENANG = "config/campaigns/penang-my.yaml"

GOOD = {
    "tone": "warm and local",
    "services": ["cut", "colour"],
    "price_tier": "mid-range",
    "customer_praise": "Friendly staff",
    "customer_pain_points": "Hard to book",
    "suggested_website_angle": "Online booking would stop the phone tag.",
}


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Returns each queued reply in turn, recording what was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.posts = []

    def post(self, url, json, headers, timeout):
        self.posts.append({"url": url, "body": json, "headers": headers})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, FakeResponse):
            return reply
        return FakeResponse(
            {"choices": [{"message": {"content": reply}}]}
        )


def writer(*replies, **kwargs):
    return OpenAICompatibleWriter(
        "key",
        base_url=kwargs.pop("base_url", "https://ollama.com/v1"),
        model=kwargs.pop("model", "kimi-k2.6"),
        session=FakeSession(*replies),
    )


# ------------------------------------------------------------- the happy path


def test_a_clean_json_reply_becomes_a_brief():
    brief = writer(_json.dumps(GOOD)).write("evidence")
    assert brief.tone == "warm and local"
    assert brief.services == ("cut", "colour")


def test_it_posts_to_the_chat_completions_endpoint():
    w = writer(_json.dumps(GOOD))
    w.write("evidence")

    post = w._session.posts[0]
    assert post["url"] == "https://ollama.com/v1/chat/completions"
    assert post["body"]["model"] == "kimi-k2.6"
    assert post["headers"]["Authorization"] == "Bearer key"


def test_it_asks_for_schema_conformance_where_the_backend_can_enforce_it():
    w = writer(_json.dumps(GOOD))
    w.write("evidence")

    fmt = w._session.posts[0]["body"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == BRIEF_SCHEMA


def test_the_schema_is_also_spelled_out_in_the_prompt():
    """Belt and braces -- a local model may ignore response_format entirely."""
    w = writer(_json.dumps(GOOD))
    w.write("evidence")

    system = w._session.posts[0]["body"]["messages"][0]["content"]
    assert "JSON object and nothing else" in system
    assert "suggested_website_angle" in system


def test_local_ollama_sends_no_authorization_header():
    w = OpenAICompatibleWriter(
        "", base_url="http://localhost:11434/v1", model="llama3",
        session=FakeSession(_json.dumps(GOOD)),
    )
    w.write("evidence")
    assert "Authorization" not in w._session.posts[0]["headers"]


# --------------------------------------------- replies that ignore the schema


def test_markdown_fences_are_unwrapped():
    reply = "```json\n" + _json.dumps(GOOD) + "\n```"
    assert writer(reply).write("evidence").tone == "warm and local"


def test_a_preamble_before_the_json_is_tolerated():
    reply = "Sure! Here is the brief you asked for:\n" + _json.dumps(GOOD)
    assert writer(reply).write("evidence").tone == "warm and local"


def test_a_missing_field_is_retried_once_and_then_succeeds():
    thin = _json.dumps({"tone": "warm"})
    w = writer(thin, _json.dumps(GOOD))

    brief = w.write("evidence")

    assert brief.tone == "warm and local"
    assert len(w._session.posts) == 2, "it should have asked once more"


def test_the_retry_names_exactly_what_was_missing():
    w = writer(_json.dumps({"tone": "warm"}), _json.dumps(GOOD))
    w.write("evidence")

    correction = w._session.posts[1]["body"]["messages"][-1]["content"]
    assert "suggested_website_angle" in correction
    assert "JSON only" in correction


def test_two_bad_replies_fail_the_lead_rather_than_writing_a_stub():
    """A half-filled brief becomes a demo with holes and an email built on air."""
    w = writer(_json.dumps({"tone": "warm"}), _json.dumps({"tone": "warm"}))

    with pytest.raises(LLMError, match="missing required field"):
        w.write("evidence")


def test_prose_instead_of_json_fails_clearly():
    w = writer("I'd be happy to help with that!", "still not json")
    with pytest.raises(LLMError, match="replied in prose"):
        w.write("evidence")


def test_an_empty_required_value_counts_as_missing():
    hollow = _json.dumps({**GOOD, "suggested_website_angle": ""})
    w = writer(hollow, hollow)

    with pytest.raises(LLMError, match="suggested_website_angle"):
        w.write("evidence")


def test_a_json_array_is_rejected():
    w = writer("[1, 2, 3]", "[1, 2, 3]")
    with pytest.raises(LLMError, match="not an object"):
        w.write("evidence")


def test_prose_error_includes_a_snippet_of_what_it_said():
    said = "Sorry, I can't produce JSON for this request right now."
    w = writer(said, said)
    with pytest.raises(LLMError, match="can't produce JSON for this request"):
        w.write("evidence")


# --------------------------------- reasoning models (DeepSeek-style replies)


def reasoning_reply(content="", **extra_fields):
    """A FakeResponse shaped like a reasoning backend's chat completion."""
    message = {"content": content, **extra_fields}
    return FakeResponse({"choices": [{"message": message}]})


def test_empty_content_falls_back_to_reasoning_content():
    reply = reasoning_reply(content="", reasoning_content=_json.dumps(GOOD))
    brief = writer(reply).write("evidence")
    assert brief.tone == "warm and local"


def test_empty_content_falls_back_to_reasoning_when_no_reasoning_content():
    reply = reasoning_reply(content="", reasoning=_json.dumps(GOOD))
    brief = writer(reply).write("evidence")
    assert brief.tone == "warm and local"


def test_content_wins_over_reasoning_content_when_both_are_present():
    """Unchanged behaviour when a model answers normally -- byte-identical."""
    reply = reasoning_reply(
        content=_json.dumps(GOOD), reasoning_content="stale draft, ignore me"
    )
    brief = writer(reply).write("evidence")
    assert brief.tone == "warm and local"


def test_reasoning_content_none_does_not_crash():
    reply = reasoning_reply(content="", reasoning_content=None)
    w = writer(reply, _json.dumps(GOOD))
    assert w.write("evidence").tone == "warm and local"


def test_reasoning_content_non_string_does_not_crash():
    reply = reasoning_reply(content="", reasoning_content={"nested": True})
    w = writer(reply, _json.dumps(GOOD))
    assert w.write("evidence").tone == "warm and local"


def test_a_fully_empty_reply_names_the_real_cause_not_reply_was_not_json():
    empty = reasoning_reply(content="")
    w = writer(empty, empty)

    with pytest.raises(LLMError) as excinfo:
        w.write("evidence")

    message = str(excinfo.value)
    assert "returned nothing" in message
    assert "non-reasoning model" in message
    assert "context/output limit" in message
    assert "test-model" in message
    assert "reply was not JSON" not in message


def test_an_empty_reply_gets_the_same_one_retry_as_any_other_bad_reply():
    """Decision: an empty reply consumes the retry rather than failing fast.

    The retry prompt asks for JSON only, no prose, no fences -- exactly the
    shorter target a budget-starved reasoning model has the best chance of
    finishing within its limit on a second attempt. Failing fast would throw
    away that chance for no benefit.
    """
    empty = reasoning_reply(content="")
    w = writer(empty, _json.dumps(GOOD))

    brief = w.write("evidence")

    assert brief.tone == "warm and local"
    assert len(w._session.posts) == 2


def test_a_think_block_with_a_stray_brace_does_not_corrupt_extraction():
    reply = (
        '<think>Let me draft this. Maybe {"tone": "wrong draft"} -- no, keep '
        "thinking.</think>\n" + _json.dumps(GOOD)
    )
    brief = writer(reply).write("evidence")
    assert brief.tone == "warm and local"


def test_an_unterminated_think_tag_does_not_swallow_the_reply():
    reply = (
        "<think>still deciding how to phrase this, never actually closes\n"
        + _json.dumps(GOOD)
    )
    brief = writer(reply).write("evidence")
    assert brief.tone == "warm and local"


# ------------------------------------------------------------- transport errors


def test_a_bad_key_says_which_env_var_to_check():
    w = writer(FakeResponse(None, status_code=401, text="unauthorized"))
    with pytest.raises(LLMError, match="OLLAMA_API_KEY"):
        w.write("evidence")


def test_a_404_suggests_pulling_the_model():
    w = writer(FakeResponse(None, status_code=404, text="model not found"))
    with pytest.raises(LLMError, match="ollama pull"):
        w.write("evidence")


def test_an_unreachable_host_names_the_url():
    w = writer(ConnectionError("refused"), base_url="http://localhost:11434/v1")
    with pytest.raises(LLMError, match="localhost:11434"):
        w.write("evidence")


def test_a_rate_limit_is_reported_as_one():
    w = writer(FakeResponse(None, status_code=429, text="slow down"))
    with pytest.raises(LLMError, match="Rate limited"):
        w.write("evidence")


# ----------------------------------------------------------------- the factory


def config(**research):
    cfg = load_config(PENANG, env_file=None)
    return replace(cfg, research=replace(cfg.research, **research))


def test_the_default_campaign_still_uses_anthropic():
    from outreach.providers.llm import BriefWriter

    cfg = config()
    cfg = replace(cfg, secrets=replace(cfg.secrets, anthropic_api_key="k"))
    assert isinstance(make_writer(cfg), BriefWriter)


def test_ollama_cloud_builds_the_compatible_writer():
    cfg = config(provider="ollama-cloud", base_url="https://ollama.com/v1",
                 model="kimi-k2.6")
    cfg = replace(cfg, secrets=replace(cfg.secrets, llm_api_key="k"))

    made = make_writer(cfg)
    assert isinstance(made, OpenAICompatibleWriter)
    assert made.model == "kimi-k2.6"


def test_local_ollama_needs_no_key():
    cfg = config(provider="ollama", base_url="http://localhost:11434/v1",
                 model="llama3")
    cfg = replace(cfg, secrets=replace(cfg.secrets, llm_api_key=""))
    assert isinstance(make_writer(cfg), OpenAICompatibleWriter)


def test_a_missing_key_says_which_one():
    cfg = config(provider="openrouter", base_url="https://openrouter.ai/api/v1",
                 model="moonshotai/kimi-k2-thinking")
    cfg = replace(cfg, secrets=replace(cfg.secrets, llm_api_key=""))

    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        make_writer(cfg)


def test_effort_is_not_sent_to_a_non_anthropic_provider():
    """It is an Anthropic parameter; other backends 400 on unknown fields."""
    w = writer(_json.dumps(GOOD))
    w.write("evidence")
    assert "effort" not in w._session.posts[0]["body"]


# ------------------------------------------------------- config-level checks


def test_every_provider_has_a_default_endpoint_or_demands_one():
    from outreach.config import PROVIDER_DEFAULTS

    assert set(PROVIDER_DEFAULTS) == {
        "anthropic", "ollama", "ollama-cloud", "openrouter", "gemini",
        "openai-compatible",
    }
    assert PROVIDER_DEFAULTS["ollama"].startswith("http://localhost")
    assert PROVIDER_DEFAULTS["ollama-cloud"] == "https://ollama.com/v1"
    assert PROVIDER_DEFAULTS["gemini"] == (
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    )


# --------------------------------------------------------------------- gemini
#
# Google's OpenAI-compatible endpoint rides the exact same writer as Ollama
# and OpenRouter -- it is just another bearer-token backend. The one thing
# worth pinning is the URL join: Google's documented base_url has a trailing
# slash where the other two don't, and a silent "//chat/completions" 404s
# for Lyon without ever explaining why.


def test_gemini_provider_loads_with_its_default_endpoint_and_key_env(tmp_path, monkeypatch):
    from outreach.config import PROVIDER_KEY_ENV, load_config as load

    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    src = open(PENANG).read().replace(
        "  provider: anthropic", "  provider: gemini", 1
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)

    cfg = load(path, env_file=None)

    assert cfg.research.provider == "gemini"
    assert cfg.research.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert PROVIDER_KEY_ENV["gemini"] == "GEMINI_API_KEY"
    assert cfg.secrets.llm_api_key == "g-key"


def test_gemini_base_url_joins_to_exactly_one_slash():
    """The documented base_url has a trailing slash; the endpoint must not.

    Goes straight at ``OpenAICompatibleWriter`` with Google's base_url exactly
    as their docs give it -- trailing slash and all -- rather than through
    config, because the join lives in the writer's constructor
    (``base_url.rstrip("/")``) and that is the one place it needs to hold for
    every caller, not just the ones that go through ``load_config``.
    """
    w = writer(
        _json.dumps(GOOD),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-2.5-flash",
    )
    w.write("evidence")

    url = w._session.posts[0]["url"]
    assert url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert "//chat/completions" not in url


def test_an_explicit_gemini_base_url_overrides_the_default(tmp_path):
    from outreach.config import load_config as load

    src = open(PENANG).read().replace(
        "  provider: anthropic", "  provider: gemini", 1
    ).replace(
        '  # base_url: ""   # override the provider\'s endpoint (a LAN Ollama box, say)',
        "  base_url: https://my-proxy.example.com/v1beta/openai/",
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)

    cfg = load(path, env_file=None)
    assert cfg.research.base_url == "https://my-proxy.example.com/v1beta/openai"


def test_gemini_missing_key_names_gemini_api_key():
    cfg = config(provider="gemini",
                 base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                 model="gemini-2.5-flash")
    cfg = replace(cfg, secrets=replace(cfg.secrets, llm_api_key=""))

    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        make_writer(cfg)


def test_an_unknown_provider_is_refused_at_load_time(tmp_path):
    from outreach.config import ConfigError, load_config as load

    src = open(PENANG).read().replace(
        "  provider: anthropic", "  provider: wishful-thinking", 1
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)

    with pytest.raises(ConfigError, match="research.provider must be one of"):
        load(path, env_file=None)


def test_openai_compatible_without_a_base_url_is_refused(tmp_path):
    from outreach.config import ConfigError, load_config as load

    src = open(PENANG).read().replace(
        "  provider: anthropic", "  provider: openai-compatible", 1
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)

    with pytest.raises(ConfigError, match="research.base_url must name"):
        load(path, env_file=None)


def test_the_key_read_from_env_follows_the_provider(tmp_path, monkeypatch):
    from outreach.config import load_config as load

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")

    src = open(PENANG).read().replace(
        "  provider: anthropic", "  provider: openrouter", 1
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)

    cfg = load(path, env_file=None)
    assert cfg.secrets.llm_api_key == "or-key"
    assert cfg.secrets.anthropic_api_key == "ant-key", "still read, still separate"


def test_the_empty_reply_message_counts_fields_from_the_schema():
    """A number typed into this string keeps printing after the schema moves.

    The message exists to be trusted at 7am when half a batch has failed, so
    both counts are read from the schemas. It also must not tell Lyon the site
    spec is harder than the call that just failed when the call that just
    failed IS the site spec.
    """
    from outreach.providers.llm import (
        LLMError,
        OUTREACH_COPY_SCHEMA,
        SITE_SPEC_SCHEMA,
        _validated,
    )

    worst = len(SITE_SPEC_SCHEMA["required"])
    small = len(OUTREACH_COPY_SCHEMA["required"])

    with pytest.raises(LLMError) as spec_err:
        _validated("", SITE_SPEC_SCHEMA)
    with pytest.raises(LLMError) as copy_err:
        _validated("", OUTREACH_COPY_SCHEMA)

    assert f"needs {worst} fields" in str(spec_err.value)
    assert "worst of it" not in str(spec_err.value)

    assert f"needs {small} fields" in str(copy_err.value)
    assert f"at {worst} required fields" in str(copy_err.value)


def test_the_empty_reply_message_follows_the_schema_when_it_moves(monkeypatch):
    """Pinning the live count cannot tell a computed 19 from a typed one.

    So move the schema and check the message moves with it. Without this the
    first assertion above passes against a hardcoded number for exactly as
    long as the real count happens to equal it.
    """
    from outreach.providers import llm

    monkeypatch.setattr(
        llm,
        "SITE_SPEC_SCHEMA",
        {**llm.SITE_SPEC_SCHEMA, "required": [f"f{i}" for i in range(25)]},
    )
    with pytest.raises(llm.LLMError) as err:
        llm._validated("", llm.OUTREACH_COPY_SCHEMA)

    assert "at 25 required fields" in str(err.value)
    assert "19" not in str(err.value)
