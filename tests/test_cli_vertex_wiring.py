"""`_image_client` and its wiring into `run site-generate`.

No network is reachable from this container, so every test here monkeypatches
`outreach.auth` and the generator/report machinery rather than touching a real
Vertex endpoint, a real Sheet, or a real OAuth consent screen.
"""

import types
from dataclasses import replace

from click.testing import CliRunner

from outreach import cli as cli_mod
from outreach.config import load_config
from outreach.providers import sitegen as sitegen_mod
from outreach.stages import site_brief as site_brief_stage

PENANG = "config/campaigns/penang-my.yaml"


def _base_config():
    return load_config(PENANG, env_file=None)


def vertex_config(**secrets_overrides):
    """Campaign config with `image_provider` and `max_images_per_run` pinned
    explicitly, not inherited -- the live campaign only ever sets these
    commented out, so a helper that read them off the file as-is would be
    testing whatever the comments happen to say."""
    cfg = _base_config()
    build = replace(
        cfg.build,
        image_provider="vertex",
        vertex_image_model="gemini-3.1-flash-image-preview",
        vertex_region="global",
        vertex_image_size="",
        max_images_per_run=60,
    )
    secrets = replace(
        cfg.secrets,
        vertex_project_id="sme-outreach",
        gemini_api_key="fake-gemini-key",
        **secrets_overrides,
    )
    return replace(cfg, build=build, secrets=secrets)


def gemini_config():
    cfg = _base_config()
    build = replace(cfg.build, image_provider="gemini", max_images_per_run=60)
    secrets = replace(cfg.secrets, gemini_api_key="fake-gemini-key")
    return replace(cfg, build=build, secrets=secrets)


class FakeCreds:
    valid = True
    token = "unused-directly"


# ------------------------------------------------------------- _image_client


def test_image_client_is_none_on_the_default_gemini_path(monkeypatch):
    """The default path must construct nothing and call no OAuth -- a user
    who never touches this feature cannot be affected by it."""

    def _boom(*_a, **_k):
        raise AssertionError(
            "vertex_credentials must never be called for image_provider=gemini"
        )

    monkeypatch.setattr("outreach.auth.vertex_credentials", _boom)

    assert cli_mod._image_client(gemini_config()) is None


def test_image_client_builds_a_vertex_client_from_config(monkeypatch):
    monkeypatch.setattr(
        "outreach.auth.vertex_credentials", lambda *a, **k: FakeCreds()
    )
    monkeypatch.setattr("outreach.auth.access_token", lambda creds: "live-token")

    client = cli_mod._image_client(vertex_config())

    assert client is not None
    assert client.project == "sme-outreach"
    assert client.model == "gemini-3.1-flash-image-preview"
    assert client.region == "global"


def test_image_client_token_provider_calls_access_token_fresh_each_time(monkeypatch):
    """An OAuth access token lives one hour; a sixty-page run outlives that.
    The client must be given a closure that calls `access_token(creds)` again
    on every request, not a bearer string captured once at construction."""
    calls = []

    def fake_access_token(creds):
        calls.append(creds)
        return f"token-{len(calls)}"

    monkeypatch.setattr(
        "outreach.auth.vertex_credentials", lambda *a, **k: FakeCreds()
    )
    monkeypatch.setattr("outreach.auth.access_token", fake_access_token)

    client = cli_mod._image_client(vertex_config())

    assert client._token_provider() == "token-1"
    assert client._token_provider() == "token-2"
    assert len(calls) == 2, "each call must ask access_token() again, not reuse one string"


# ------------------------------------------------------- run site-generate


def test_gemini_api_key_is_required_before_any_vertex_setup_is_touched(monkeypatch):
    """GEMINI_API_KEY still writes the page regardless of image_provider, and
    that check must come before anything that would start an OAuth flow."""
    cfg = replace(vertex_config(), secrets=replace(vertex_config().secrets, gemini_api_key=""))
    monkeypatch.setattr(cli_mod, "_load", lambda _path: cfg)

    def _boom(_config):
        raise AssertionError("_image_client must not run before the GEMINI_API_KEY check")

    monkeypatch.setattr(cli_mod, "_image_client", _boom)

    result = CliRunner().invoke(cli_mod.main, ["run", "site-generate"])

    assert result.exit_code != 0
    assert "GEMINI_API_KEY" in result.output


def test_run_site_generate_passes_the_vertex_client_into_the_generator(monkeypatch):
    """The whole point of the wiring: whatever `_image_client` returns must be
    the exact object `SiteGenerator` receives as `image_client`."""
    fake_client = types.SimpleNamespace(
        model="gemini-3.1-flash-image-preview", project="sme-outreach", region="global"
    )

    monkeypatch.setattr(cli_mod, "_load", lambda _path: vertex_config())
    monkeypatch.setattr(cli_mod, "_sheets_client", lambda _config: object())
    monkeypatch.setattr(cli_mod, "make_writer", lambda _config: object())
    monkeypatch.setattr(cli_mod, "_image_client", lambda _config: fake_client)

    captured = {}

    class FakeGenerator:
        def __init__(self, _api_key, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(sitegen_mod, "SiteGenerator", FakeGenerator)

    class FakeReport:
        def summary_lines(self):
            return ["ok"]

    monkeypatch.setattr(
        site_brief_stage, "generate_sites", lambda *a, **k: FakeReport()
    )

    result = CliRunner().invoke(cli_mod.main, ["run", "site-generate"])

    assert result.exit_code == 0, result.output
    assert captured["image_client"] is fake_client


def test_the_vertex_banner_names_model_project_region_and_the_cap(monkeypatch):
    """Lyon runs this from a terminal he does not otherwise use -- the banner
    is how he knows this run is spending Cloud credits, not the free tier."""
    fake_client = types.SimpleNamespace(
        model="gemini-3.1-flash-image-preview", project="sme-outreach", region="global"
    )

    monkeypatch.setattr(cli_mod, "_load", lambda _path: vertex_config())
    monkeypatch.setattr(cli_mod, "_sheets_client", lambda _config: object())
    monkeypatch.setattr(cli_mod, "make_writer", lambda _config: object())
    monkeypatch.setattr(cli_mod, "_image_client", lambda _config: fake_client)
    monkeypatch.setattr(
        sitegen_mod, "SiteGenerator", lambda _api_key, **_k: object()
    )

    class FakeReport:
        def summary_lines(self):
            return []

    monkeypatch.setattr(
        site_brief_stage, "generate_sites", lambda *a, **k: FakeReport()
    )

    result = CliRunner().invoke(cli_mod.main, ["run", "site-generate"])

    assert result.exit_code == 0, result.output
    assert "Vertex" in result.output
    assert "gemini-3.1-flash-image-preview" in result.output
    assert "sme-outreach" in result.output
    assert "global" in result.output
    assert "60" in result.output
    assert "Google Cloud credits" in result.output


def test_no_vertex_banner_on_the_default_gemini_path(monkeypatch):
    """The banner is specific to spending Cloud credits -- printing it when
    nothing is being spent would be a false alarm."""
    monkeypatch.setattr(cli_mod, "_load", lambda _path: gemini_config())
    monkeypatch.setattr(cli_mod, "_sheets_client", lambda _config: object())
    monkeypatch.setattr(cli_mod, "make_writer", lambda _config: object())
    monkeypatch.setattr(
        sitegen_mod, "SiteGenerator", lambda _api_key, **_k: object()
    )

    class FakeReport:
        def summary_lines(self):
            return []

    monkeypatch.setattr(
        site_brief_stage, "generate_sites", lambda *a, **k: FakeReport()
    )

    result = CliRunner().invoke(cli_mod.main, ["run", "site-generate"])

    assert result.exit_code == 0, result.output
    assert "Vertex" not in result.output
    assert "Google Cloud credits" not in result.output
