"""`outreach vertex-check` -- the one command that proves the Vertex chain.

Nothing here reaches Google. The client is replaced with a stub, because the
point of these tests is the *ordering and wording* of what Lyon reads when a
step fails, not whether Vertex answers.
"""

import pathlib
from dataclasses import replace

from click.testing import CliRunner

from outreach.config import load_config

# Absolute: these tests chdir into tmp_path so the image lands there, and a
# relative campaign path would resolve against the wrong directory.
PENANG = str(pathlib.Path("config/campaigns/penang-my.yaml").resolve())


def _config(**build):
    cfg = load_config(PENANG, env_file=None)
    return replace(
        cfg,
        build=replace(
            cfg.build,
            **{
                "image_provider": "vertex",
                "vertex_image_model": "some-model",
                **build,
            },
        ),
        secrets=replace(cfg.secrets, vertex_project_id="sme-outreach-504919"),
    )


def _run_check(client, *, config=None, args=()):
    """Run vertex-check with the network replaced by `client`."""
    from outreach import cli

    cfg = config or _config()
    runner = CliRunner()

    original_load, original_client = cli._load, cli._image_client
    cli._load = lambda path: cfg
    # Record the config the command actually built the client from, so a test
    # can assert on a flag that only shows up there (like --region).
    def _stub(c, require_model=True):
        client.seen_config = c
        return client

    cli._image_client = _stub
    try:
        return runner.invoke(cli.main, ["vertex-check", *args])
    finally:
        cli._load, cli._image_client = original_load, original_client


class _Ok:
    model = "some-model"
    endpoint = "https://example/vertex"

    def list_models(self):
        return ["gemini-3.6-flash", "gemini-2.5-flash-image"]

    def generate_image(self, prompt):
        return b"\x89PNG-bytes", "image/png"


def test_a_working_chain_reports_every_step_and_writes_the_image(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = _run_check(_Ok())

    assert result.exit_code == 0, result.output
    assert "[ OK ] VERTEX_PROJECT_ID is set" in result.output
    assert "[ OK ] Signed in for Google Cloud" in result.output
    assert "[ OK ] Vertex reachable" in result.output
    assert "[ OK ] Image generated" in result.output
    assert (tmp_path / ".cache" / "vertex-check.png").read_bytes() == b"\x89PNG-bytes"


def test_it_names_the_campaign_file_it_actually_loaded(tmp_path, monkeypatch):
    """If the dashboard has ever saved a setting the live file is the .local
    twin, and editing the tracked one is a silent no-op that looks exactly
    like broken code. The first line has to say which file is real."""
    monkeypatch.chdir(tmp_path)

    result = _run_check(_Ok())

    assert "Campaign config" in result.output
    assert "penang-my" in result.output


def test_it_says_the_sheets_and_gmail_login_is_untouched(tmp_path, monkeypatch):
    """Lyon's stated fear about this whole change. He should read the
    reassurance at the moment he signs in, not in a doc he may not open."""
    monkeypatch.chdir(tmp_path)

    result = _run_check(_Ok())

    assert "Sheets and Gmail login is a different file" in result.output


def test_an_unknown_model_prints_the_names_the_project_does_have(tmp_path, monkeypatch):
    """The reachability check has already fetched the list. Making Lyon run a
    second command to see names we are holding is a round trip for nothing --
    and it is exactly the dead end he hit twice with guessed model names."""
    monkeypatch.chdir(tmp_path)

    from outreach.providers.sitegen import SiteGenError

    class Unknown(_Ok):
        def list_models(self):
            return [
                "gemini-3.6-flash",
                "imagen-4.0-generate-001",
                "gemini-2.5-flash-image",
            ]

        def generate_image(self, prompt):
            raise SiteGenError("Vertex has no model called 'nope'", status_code=404)

    result = _run_check(Unknown())

    assert "Google publishes these image models" in result.output
    assert "imagen-4.0-generate-001" in result.output
    assert "gemini-2.5-flash-image" in result.output
    # The text model is not an image model and must not be offered as one.
    assert "  gemini-3.6-flash\n" not in result.output
    assert "outreach vertex-check --model" in result.output
    # A 404 on a name that IS in the list means the model is not served in
    # this region -- saying "your project offers these" would send him
    # hunting for the wrong problem.
    assert "not served in region" in result.output
    assert "--region us-central1" in result.output


def test_a_missing_project_id_stops_before_the_confusing_auth_error(tmp_path, monkeypatch):
    """Ordering is the whole point: an auth failure reported before the real
    cause sends him to fix the wrong thing."""
    monkeypatch.chdir(tmp_path)

    cfg = _config()
    cfg = replace(cfg, secrets=replace(cfg.secrets, vertex_project_id=""))

    result = _run_check(_Ok(), config=cfg)

    assert result.exit_code != 0
    assert "[FAIL] VERTEX_PROJECT_ID is not set" in result.output
    assert "[ OK ] Signed in" not in result.output


def test_the_region_can_be_tried_without_editing_the_campaign_file(tmp_path, monkeypatch):
    """Region is the likely fix for a 404 on a published model, so trying one
    must not cost a YAML edit and a reload."""
    monkeypatch.chdir(tmp_path)

    client = _Ok()
    result = _run_check(client, args=("--region", "US-Central1"))

    assert result.exit_code == 0, result.output
    assert client.seen_config.build.vertex_region == "us-central1", (
        "the flag must reach the client, lowercased"
    )
