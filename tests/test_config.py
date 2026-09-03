import pytest

from outreach.config import IMAGE_PROVIDERS, ConfigError, load_config

PENANG = "config/campaigns/penang-my.yaml"
UK = "config/campaigns/example-manchester-uk.yaml"


def test_loads_penang_campaign(monkeypatch):
    monkeypatch.setenv("PLACES_API_KEY", "test-key")
    monkeypatch.setenv("SHEET_ID", "sheet-123")
    config = load_config(PENANG, env_file=None)

    assert config.campaign.name == "penang-hair-salons"
    assert config.campaign.search_query == "hair salon in Georgetown, Penang, Malaysia"
    assert config.compliance.country_code == "MY"
    assert config.secrets.places_api_key == "test-key"
    assert config.secrets.sheet_id == "sheet-123"
    assert "facebook.com" in config.discovery.social_domains


def test_region_swap_needs_no_code_change():
    uk = load_config(UK, env_file=None)
    assert uk.compliance.country_code == "GB"
    assert uk.campaign.search_query == "barber shop in Manchester, United Kingdom"


def test_outreach_model_defaults_to_blank():
    """Blank means 'use research.model' -- the campaign file does not set it."""
    config = load_config(PENANG, env_file=None)
    assert config.outreach.model == ""


def test_outreach_model_loads_from_yaml(tmp_path):
    path = tmp_path / "small.yaml"
    path.write_text(
        "campaign:\n  name: x\n  category: y\n  location: z\n"
        "compliance:\n  country_code: MY\n"
        "outreach:\n  model: qwen3:cloud\n"
    )
    config = load_config(path, env_file=None)
    assert config.outreach.model == "qwen3:cloud"


def test_secrets_never_come_from_yaml(monkeypatch):
    monkeypatch.delenv("PLACES_API_KEY", raising=False)
    config = load_config(PENANG, env_file=None)
    assert config.secrets.places_api_key == ""


def test_rejects_page_size_above_places_cap(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "campaign:\n"
        "  name: x\n  category: y\n  location: z\n"
        "discovery:\n  page_size: 50\n"
        "compliance:\n  country_code: MY\n"
    )
    with pytest.raises(ConfigError, match="page_size"):
        load_config(path, env_file=None)


def test_country_code_is_required(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("campaign:\n  name: x\n  category: y\n  location: z\n")
    with pytest.raises(ConfigError, match="country_code"):
        load_config(path, env_file=None)


def test_missing_campaign_field_names_the_file(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("campaign:\n  name: x\ncompliance:\n  country_code: MY\n")
    with pytest.raises(ConfigError, match="category"):
        load_config(path, env_file=None)


# --------------------------------------------------------------------- build


def test_build_defaults_keep_lovable_the_live_target():
    """Byte-identical today's behaviour: an untouched campaign still builds
    through Lovable, and never turns on the Firebase spend on its own."""
    config = load_config(PENANG, env_file=None)
    assert config.build.target == "lovable"
    assert config.build.enabled is False


def test_build_defaults_are_pinned():
    config = load_config(PENANG, env_file=None)
    assert config.build.provider == "gemini"
    assert config.build.model == "gemini-3.6-flash"
    assert config.build.max_output_tokens == 64000
    assert config.build.min_interval_seconds == 6.5
    assert config.build.output_dir == "sites"
    assert config.build.url_context is False
    assert config.build.public_base_url == ""
    assert config.build.max_versions == 5
    assert config.build.min_bytes == 4000
    assert config.build.max_bytes == 400000
    assert config.build.site_builder_name == "Google AI Studio"
    assert config.build.site_builder_url == "https://aistudio.google.com/"


def test_build_max_output_tokens_is_not_the_json_writer_cap():
    """32000 is deliberate -- reusing the JSON writers' 6000 truncates a
    full HTML page mid-document. See the comment in config.py for why."""
    config = load_config(PENANG, env_file=None)
    assert config.build.max_output_tokens != 6000


def test_build_site_builder_defaults_pin_google_ai_studio():
    """An untouched campaign shows the tool Lyon actually pastes prompts
    into today, so the live YAML never has to name it."""
    config = load_config(PENANG, env_file=None)
    assert config.build.site_builder_name == "Google AI Studio"
    assert config.build.site_builder_url == "https://aistudio.google.com/"


def test_build_site_builder_name_can_be_overridden(tmp_path):
    src = open(PENANG).read().replace(
        "  target: lovable\n", "  target: lovable\n  site_builder_name: Bolt.new\n", 1
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)
    config = load_config(path, env_file=None)
    assert config.build.site_builder_name == "Bolt.new"


def test_build_site_builder_url_can_be_overridden(tmp_path):
    src = open(PENANG).read().replace(
        "  target: lovable\n",
        "  target: lovable\n  site_builder_url: https://bolt.new/\n",
        1,
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)
    config = load_config(path, env_file=None)
    assert config.build.site_builder_url == "https://bolt.new/"


def test_build_site_builder_name_empty_is_rejected(tmp_path):
    src = open(PENANG).read().replace(
        "  target: lovable\n", "  target: lovable\n  site_builder_name: ''\n", 1
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)
    with pytest.raises(ConfigError, match="site_builder_name is empty"):
        load_config(path, env_file=None)


def test_build_site_builder_url_must_be_http(tmp_path):
    src = open(PENANG).read().replace(
        "  target: lovable\n", "  target: lovable\n  site_builder_url: bolt.new\n", 1
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)
    with pytest.raises(ConfigError, match=r"must be a full http\(s\) URL"):
        load_config(path, env_file=None)


def test_build_target_rejects_an_unknown_value(tmp_path):
    src = open(PENANG).read().replace("  target: lovable", "  target: wix", 1)
    path = tmp_path / "c.yaml"
    path.write_text(src)
    with pytest.raises(ConfigError, match="build.target must be one of.*firebase.*lovable"):
        load_config(path, env_file=None)


def test_firebase_target_requires_the_site_id_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("FIREBASE_SITE_ID", raising=False)
    src = open(PENANG).read().replace("  target: lovable", "  target: firebase", 1)
    path = tmp_path / "c.yaml"
    path.write_text(src)
    with pytest.raises(ConfigError, match="FIREBASE_SITE_ID") as excinfo:
        load_config(path, env_file=None)
    assert "Firebase console" in str(excinfo.value)


def test_firebase_target_loads_once_the_site_id_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREBASE_SITE_ID", "my-demo-site")
    src = (
        open(PENANG)
        .read()
        .replace("  target: lovable", "  target: firebase", 1)
        .replace("  enabled: false\n  # provider: gemini", "  enabled: true\n  # provider: gemini", 1)
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)
    config = load_config(path, env_file=None)
    assert config.build.target == "firebase"
    assert config.build.enabled is True
    assert config.secrets.firebase_site_id == "my-demo-site"


def test_build_secrets_never_come_from_yaml(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    config = load_config(PENANG, env_file=None)
    assert config.secrets.gemini_api_key == ""
    assert config.secrets.firebase_project_id == ""


# ------------------------------------------------------------- vertex images


def test_vertex_image_defaults_keep_gemini_the_live_provider(monkeypatch):
    """Byte-identical today's behaviour: an untouched campaign still draws
    images through the Gemini Developer API, never Vertex, on its own."""
    monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
    config = load_config(PENANG, env_file=None)
    assert config.build.image_provider == "gemini"
    assert config.build.vertex_image_model == ""
    assert config.build.vertex_region == "global"
    assert config.build.vertex_image_size == ""
    assert config.build.max_images_per_run == 60
    assert config.secrets.vertex_project_id == ""


def test_image_providers_is_gemini_and_vertex_only():
    assert IMAGE_PROVIDERS == frozenset({"gemini", "vertex"})


def test_image_provider_rejects_an_unknown_value(tmp_path):
    src = open(PENANG).read().replace("  # image_provider: gemini", "  image_provider: vertexai", 1)
    path = tmp_path / "c.yaml"
    path.write_text(src)
    with pytest.raises(
        ConfigError,
        match=r"build.image_provider must be one of \['gemini', 'vertex'\], got 'vertexai'",
    ):
        load_config(path, env_file=None)


def test_vertex_image_provider_requires_the_project_id_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
    src = (
        open(PENANG)
        .read()
        .replace("  # image_provider: gemini", "  image_provider: vertex", 1)
        .replace("  # vertex_image_model: \"\"", "  vertex_image_model: imagen-4.0-generate-001", 1)
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)
    with pytest.raises(ConfigError, match="VERTEX_PROJECT_ID") as excinfo:
        load_config(path, env_file=None)
    # Names the fix precisely enough that Lyon doesn't paste in the project
    # number or the display name by mistake -- both look plausible and both
    # are wrong.
    assert "project picker" in str(excinfo.value)
    assert "not the number" in str(excinfo.value) or "not the project number" in str(excinfo.value)


def test_a_missing_vertex_model_does_not_block_the_command_that_finds_one(
    tmp_path, monkeypatch
):
    """The model check is deliberately NOT at load time.

    `vertex-models` is how you discover a model name, and it has to load the
    config to know the project and region. Refusing to load without a model
    would mean the only way to satisfy the check is a command the check
    forbids you to run -- which is exactly the dead end Lyon hit.
    """
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sme-outreach-482913")
    src = open(PENANG).read().replace(
        "  # image_provider: gemini", "  image_provider: vertex", 1
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)

    config = load_config(path, env_file=None)

    assert config.build.image_provider == "vertex"
    assert config.build.vertex_image_model == ""


def test_the_model_is_required_where_it_is_actually_used(monkeypatch):
    """Moved, not dropped: generating still refuses without a named model."""
    from dataclasses import replace

    import click

    from outreach import cli

    config = load_config(PENANG, env_file=None)
    config = replace(
        config,
        build=replace(config.build, image_provider="vertex", vertex_image_model=""),
        secrets=replace(config.secrets, vertex_project_id="p"),
    )

    with pytest.raises(click.ClickException) as err:
        cli._image_client(config)

    assert "vertex-models" in str(err.value)


def test_vertex_image_provider_loads_once_project_and_model_are_set(tmp_path, monkeypatch):
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sme-outreach-482913")
    src = (
        open(PENANG)
        .read()
        .replace("  # image_provider: gemini", "  image_provider: vertex", 1)
        .replace("  # vertex_image_model: \"\"", "  vertex_image_model: imagen-4.0-generate-001", 1)
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)
    config = load_config(path, env_file=None)
    assert config.build.image_provider == "vertex"
    assert config.build.vertex_image_model == "imagen-4.0-generate-001"
    assert config.secrets.vertex_project_id == "sme-outreach-482913"


def test_vertex_region_is_normalised_to_lowercase_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sme-outreach-482913")
    src = (
        open(PENANG)
        .read()
        .replace("  # image_provider: gemini", "  image_provider: vertex", 1)
        .replace("  # vertex_image_model: \"\"", "  vertex_image_model: imagen-4.0-generate-001", 1)
        .replace("  # vertex_region: global", "  vertex_region: ' Global '", 1)
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)
    config = load_config(path, env_file=None)
    assert config.build.vertex_region == "global"


def test_max_images_per_run_rejects_negative(tmp_path):
    src = open(PENANG).read().replace("  # max_images_per_run: 60", "  max_images_per_run: -1", 1)
    path = tmp_path / "c.yaml"
    path.write_text(src)
    with pytest.raises(ConfigError, match="max_images_per_run"):
        load_config(path, env_file=None)


def test_vertex_project_id_never_comes_from_yaml(monkeypatch):
    monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
    config = load_config(PENANG, env_file=None)
    assert config.secrets.vertex_project_id == ""
