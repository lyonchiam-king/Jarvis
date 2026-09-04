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
    SiteSpec,
    make_writer,
    render_lovable_prompt,
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


def test_a_model_override_swaps_only_the_model():
    """The outreach stage passes a smaller model; provider and key stay put."""
    cfg = config(provider="ollama-cloud", base_url="https://ollama.com/v1",
                 model="qwen3.5:cloud")
    cfg = replace(cfg, secrets=replace(cfg.secrets, llm_api_key="k"))

    made = make_writer(cfg, model="qwen3:cloud")
    assert isinstance(made, OpenAICompatibleWriter)
    assert made.model == "qwen3:cloud"


def test_a_blank_model_override_falls_back_to_research_model():
    cfg = config(provider="ollama-cloud", base_url="https://ollama.com/v1",
                 model="qwen3.5:cloud")
    cfg = replace(cfg, secrets=replace(cfg.secrets, llm_api_key="k"))

    made = make_writer(cfg, model="")
    assert made.model == "qwen3.5:cloud"


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


# ---------------------------------------------------- build.target: html vs lovable
#
# Lovable is being kept, not replaced -- render_lovable_prompt must produce
# byte-identical output for target="lovable" after this change, and a
# genuinely different prompt for target="html" that never asks for a
# React/Framer Motion/shadcn stack, and never asks for a photograph it can
# neither legally cache nor physically fetch in a one-shot call.

_TARGET_SPEC = SiteSpec(
    site_goal="Get a booking",
    pages=("Home",),
    hero_headline="Fresh cuts, fast",
    hero_subcopy="Walk in or book ahead",
    key_sections=(
        "Hero -- overlay -- headline and CTA",
        "Services -- card grid -- our cuts",
    ),
    primary_cta="Book Now",
    visual_direction="Warm and modern, a neighbourhood spot",
    visitor="A commuter on their phone deciding where to get a haircut tonight",
    proof_points=("4.8 stars from 120 reviews",),
    highlights=("Walk-ins Welcome", "Open Late", "Cash or Card"),
    showcase_items=("Classic Cut -- sharp, fast", "Beard Trim -- precise, quick"),
    interactive_feature="A three-step style picker that ends at Book Now",
    mobile_bar=("Call -> tel: link", "WhatsApp -> wa.me link"),
    palette="#1a1a1a background, #f5f5f5 text, #d4a373 accent",
    typography="Playfair Display for headings, Inter for body",
    avoid=("No stock photos of models",),
    signature_moment="A before/after that wipes as you drag",
    motion_language="Snappy, 200ms, ease-out",
    micro_interactions=("CTA -> scale down on press",),
)


def _rendered(**kwargs):
    return render_lovable_prompt(
        business_name="Sun Salon",
        category="hair salon",
        location="Georgetown, Penang, Malaysia",
        spec=_TARGET_SPEC,
        phone="+60123456789",
        address="123 Jalan Example, Georgetown",
        social_handle="@sunsalon",
        social_profiles="Instagram: @sunsalon -- https://instagram.com/sunsalon",
        maps_url="https://maps.google.com/?cid=12345",
        **kwargs,
    )


def test_lovable_is_the_default_target():
    assert _rendered() == _rendered(target="lovable")


def test_lovable_target_is_byte_identical_to_before_build_target_existed():
    """The one test that stops a harmless-looking reword breaking the path
    Lyon is keeping.

    A real snapshot, not a handful of substring checks. The earlier version of
    this test asserted nine specific phrases and passed happily when a stray
    space was added to the Lovable output -- which is precisely the class of
    change it was named to catch. If this fails and the change was deliberate,
    regenerate the fixture and read the diff before you commit it.
    """
    import pathlib

    fixture = pathlib.Path(__file__).parent / "fixtures" / "lovable_prompt.txt"
    assert _rendered(target="lovable") == fixture.read_text(), (
        "the lovable prompt changed -- diff tests/fixtures/lovable_prompt.txt"
    )


def test_the_lovable_snapshot_still_contains_its_load_bearing_parts():
    """Guards the fixture itself: a regenerated-by-accident snapshot would
    make the test above pass against whatever it was regenerated from."""
    prompt = _rendered(target="lovable")
    assert "Stack: React + Tailwind + shadcn/ui." in prompt
    assert "whileInView, viewport={{ once: true }}" in prompt
    assert "useReducedMotion()" in prompt
    assert "IMAGERY" in prompt
    assert "Google Sheets connector" in prompt
    assert "NO PHOTOGRAPHY" not in prompt
    assert "OUTPUT FORMAT" not in prompt
    assert "Unofficial concept by COAI" not in prompt


def test_html_target_names_none_of_the_lovable_specific_vocabulary():
    prompt = _rendered(target="html")
    forbidden = (
        "Framer Motion", "whileInView", "AnimatePresence", "layoutId",
        "useReducedMotion", "shadcn", "motion.", "Google Sheets connector",
        "IMAGERY",
    )
    for word in forbidden:
        assert word not in prompt, f"{word!r} leaked into the html-target prompt"


def test_html_target_uses_css_and_intersection_observer_instead():
    prompt = _rendered(target="html")
    assert "@keyframes" in prompt
    assert "IntersectionObserver" in prompt
    assert "<dialog>" in prompt
    assert "prefers-reduced-motion" in prompt


def test_html_target_forbids_a_backend_and_a_fake_form():
    prompt = _rendered(target="html")
    assert "No backend exists for this site" in prompt
    assert "tel:" in prompt and "mailto:" in prompt
    assert "appears to submit" in prompt


def test_html_target_forbids_photography_and_offers_a_maps_embed_instead():
    prompt = _rendered(target="html")
    assert "NO PHOTOGRAPHY" in prompt
    assert "No third-party photograph" in prompt
    after = prompt.split("NO PHOTOGRAPHY", 1)[1][:1200]
    assert "palette" in after
    assert "maps/embed/v1/place" in after


def test_html_target_never_says_imagery_even_with_a_maps_source():
    """The showcase-card note used to point at an IMAGERY section that no
    longer exists on this target -- it must fall back to the tinted-block
    wording instead, the same wording used when there is no source at all."""
    prompt = _rendered(target="html")
    assert "IMAGERY" not in prompt
    assert "solid tinted block from the palette" in prompt


def test_html_target_restricts_image_and_script_domains_and_form_actions():
    prompt = _rendered(target="html")
    assert "fonts.googleapis.com" in prompt
    assert "fonts.gstatic.com" in prompt
    assert "No <form> with an action attribute" in prompt


def test_html_target_footer_is_fixed_text_naming_the_business():
    """Rendered by the renderer, not the model -- what turns a complaint
    into a reply-and-done email."""
    prompt = _rendered(target="html")
    assert (
        "Unofficial concept by COAI -- not affiliated with Sun Salon. "
        "Reply to have it removed." in prompt
    )


def test_html_target_requires_noindex():
    prompt = _rendered(target="html")
    assert '<meta name="robots" content="noindex">' in prompt


def test_html_target_states_the_output_format():
    prompt = _rendered(target="html")
    assert "<!doctype html>" in prompt
    assert "</html>" in prompt
    assert "no markdown" in prompt.lower()


def test_an_unknown_target_is_refused():
    with pytest.raises(ValueError, match="Unknown build target"):
        _rendered(target="wix")


def test_signature_moment_schema_no_longer_names_framer_motion():
    """Framer Motion is a Lovable implementation detail; it does not belong
    in a design brief the model writes before either target is chosen. The
    required-field count must not move -- this is a wording-only change."""
    from outreach.providers.llm import SITE_SPEC_SCHEMA

    description = SITE_SPEC_SCHEMA["properties"]["signature_moment"]["description"]
    assert "Framer Motion" not in description
    assert "achievable in CSS" in description
    assert len(SITE_SPEC_SCHEMA["required"]) == 19


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


# --- The Front-End Design Checklist upgrade ----------------------------------
#
# Lyon judged the generated pages too plain to show a client and asked for a
# more UI-focused, per-business result, pointing at thedaviddias'
# Front-End Design Checklist. Two kinds of change came out of that, and these
# tests hold the line between them: what varies per business is a spec field,
# what is identical for every business is fixed template text.


def _with_layout(layout, **kwargs):
    """_rendered() pins spec=_TARGET_SPEC, so it cannot take a spec override."""
    from dataclasses import replace as _replace

    return render_lovable_prompt(
        business_name="Sun Salon",
        category="hair salon",
        location="Georgetown, Penang, Malaysia",
        spec=_replace(_TARGET_SPEC, layout_system=layout),
        phone="+60123456789",
        address="123 Jalan Example, Georgetown",
        social_handle="@sunsalon",
        social_profiles="Instagram: @sunsalon -- https://instagram.com/sunsalon",
        maps_url="https://maps.google.com/?cid=12345",
        **kwargs,
    )


def test_the_layout_decision_reaches_the_prompt():
    """`layout_system` is the field that makes two sites structurally
    different rather than just differently coloured. If it does not render,
    the model spent tokens deciding a grid nobody was told about."""
    prompt = _with_layout("1180px, 12 columns, 24px gutter")

    assert "Layout:  1180px, 12 columns, 24px gutter" in prompt


def test_the_layout_line_is_placed_before_the_palette():
    """Structure before surface. A builder that meets the palette first tends
    to recolour its default centred column instead of building the grid it was
    given, which is the exact failure this field exists to stop."""
    prompt = _with_layout("Asymmetric editorial split")

    assert prompt.index("Layout:") < prompt.index("Palette:")


def test_a_spec_without_a_layout_renders_exactly_as_it_did_before():
    """The field is optional, so the graceful path is the common one: a model
    that skips it must produce today's prompt, not a prompt with a dangling
    empty heading in it."""
    prompt = _rendered()  # _TARGET_SPEC leaves layout_system at ""

    assert "Layout:" not in prompt
    assert "ART DIRECTION\nPalette:" in prompt


def test_layout_system_is_not_a_required_field():
    """A ratchet with a reason, not a preference. The 19 required fields are
    capped because a 20th costs every weaker non-Anthropic model the entire
    spec -- Lyon runs Ollama -- while an optional field costs a weak model
    nothing and still buys a designed layout from a capable one."""
    from outreach.providers.llm import SITE_SPEC_SCHEMA

    assert "layout_system" in SITE_SPEC_SCHEMA["properties"]
    assert "layout_system" not in SITE_SPEC_SCHEMA["required"]
    assert len(SITE_SPEC_SCHEMA["required"]) == 19


def test_every_target_says_how_to_declare_its_tokens():
    """The rule is the same for both builders; only the mechanism differs. A
    target added later with no tokens_line would render a blank line under a
    DESIGN SYSTEM heading and silently lose the section."""
    from outreach.providers.llm import TARGETS

    for name, profile in TARGETS.items():
        assert profile.tokens_line.strip(), f"{name} has no tokens_line"


def test_the_token_vocabulary_matches_the_builder_it_is_aimed_at():
    """Telling a one-file HTML page to edit tailwind.config is an instruction
    it cannot follow, and the builder will improvise something instead."""
    html = _rendered(target="html")
    lovable = _rendered(target="lovable")

    assert ":root" in html and "custom propert" in html
    assert "tailwind.config" not in html

    assert "Tailwind theme" in lovable
    assert ":root" not in lovable


def test_the_interaction_states_block_demands_a_visible_focus_ring():
    """The checklist's states section is worth almost nothing if the builder
    satisfies it with `outline: none`, which is the single most common way a
    site becomes unusable by keyboard."""
    from outreach.providers.llm import TARGETS

    for target in TARGETS:
        prompt = _rendered(target=target)
        assert "INTERACTION STATES" in prompt
        assert "focus-visible" in prompt
        assert "`outline: none`" in prompt


def test_the_states_rule_covers_more_than_the_primary_button():
    """The motion floor already handles the CTA's press state. This block
    earns its place only by generalising past that one element."""
    prompt = _rendered()
    states = prompt.split("INTERACTION STATES", 1)[1]

    assert "Every link, button and input" in states
    assert "nav item" in states


# ------------------------------------------------- the showcase design level


def test_standard_is_the_default_and_is_unchanged_by_the_switch():
    """The switch must be additive. Everything built before it existed was
    built at this level, and a silent change to it would make an old brief
    and a new one incomparable."""
    assert _rendered() == _rendered(design_level="standard")


def test_showcase_only_adds():
    """The craft blocks are extra art direction, not a rewrite. Everything
    the standard prompt says still has to be said."""
    standard = _rendered(design_level="standard")
    showcase = _rendered(design_level="showcase")

    assert len(showcase) > len(standard)
    # Every non-trivial line of the standard prompt survives verbatim.
    missing = [
        line for line in standard.splitlines()
        if len(line.strip()) > 20 and line not in showcase
    ]
    assert not missing, f"showcase dropped standard lines: {missing[:3]}"


def test_showcase_carries_the_craft_blocks():
    showcase = _rendered(design_level="showcase")

    for heading in (
        "TYPE AS A SYSTEM",
        "LAYOUT (break the centred column",
        "COLOUR DISCIPLINE",
        "MOTION, IN NUMBERS",
    ):
        assert heading in showcase, f"{heading} missing from the showcase prompt"


def test_the_craft_blocks_are_absent_at_standard():
    standard = _rendered(design_level="standard")

    for heading in ("TYPE AS A SYSTEM", "COLOUR DISCIPLINE", "MOTION, IN NUMBERS"):
        assert heading not in standard


def test_showcase_names_real_numbers_not_adjectives():
    """The whole point of the motion block: "slow" becomes a different guess
    in every component, so the prompt states durations."""
    showcase = _rendered(design_level="showcase")

    assert "120-200ms" in showcase
    assert "400-700ms" in showcase
    assert "60-80ms" in showcase


def test_each_builder_gets_its_own_component_warning():
    """Opposite risks: shadcn ships a recognisable default look, and a
    hand-rolled page ships the browser's."""
    assert "shadcn's defaults are recognisable" in _rendered(
        target="lovable", design_level="showcase")
    assert "::backdrop" in _rendered(target="html", design_level="showcase")


def test_an_unknown_design_level_is_refused_by_name():
    import pytest

    with pytest.raises(ValueError) as exc:
        _rendered(design_level="fancy")
    assert "showcase" in str(exc.value)


def test_the_showcase_snapshots_are_stable():
    """Same guard as the standard prompt: a reworded craft block is a real
    change to every site built afterwards, so it is read as a diff."""
    import pathlib

    here = pathlib.Path(__file__).parent / "fixtures"
    for target, name in (("lovable", "lovable_prompt_showcase.txt"),
                         ("html", "html_prompt_showcase.txt")):
        assert _rendered(target=target, design_level="showcase") == (
            here / name).read_text(), f"diff tests/fixtures/{name}"


def test_the_mobile_bar_clears_the_iphone_home_indicator():
    """Both levels. A bar under the gesture bar is a bug, not a flourish --
    it sits exactly where the thumb already is."""
    for level in ("standard", "showcase"):
        assert "safe-area-inset-bottom" in _rendered(design_level=level)
