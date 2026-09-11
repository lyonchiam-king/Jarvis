"""The image the pipeline runs in when it is not on Lyon's PC.

Nothing here builds a container: the Docker daemon is not available in the
environment this was written in, so every claim below is checked by reading
the files and by installing the package for real. What that leaves unproven
is stated in CLAUDE.md rather than implied away -- the first real `docker
build` is Lyon's.

The two bugs this file exists because of were both found by reading:
installing with only `pyproject.toml` present would have produced an image
whose `outreach` command did not exist, and the built React page was not
declared as package data, so `/app` would have 404'd from an installed copy
with nothing in the log to explain it.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

DOCKERFILE = pathlib.Path("Dockerfile")
IGNORE = pathlib.Path(".dockerignore")


def directives(name: str) -> list[str]:
    """Whole Dockerfile instructions of one kind, comments stripped.

    Continuations are joined. A RUN that installs four packages across four
    lines is one instruction, and reading only its first line made a test
    report a missing package that was plainly there on the next.
    """
    joined, current = [], ""
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current += stripped
        if stripped.endswith("\\"):
            current = current[:-1] + " "
            continue
        joined.append(current)
        current = ""
    if current:
        joined.append(current)
    return [line for line in joined if line.upper().startswith(name.upper() + " ")]


# ------------------------------------------------- what the image must hold


def test_a_browser_the_screenshotter_will_actually_find():
    """Installing a browser the code cannot locate is worse than none.

    `screenshot.py` drives whatever it finds on PATH rather than installing
    one, so the package name and the search list have to agree.
    """
    from outreach.screenshot import _PATH_NAMES

    installed = "\n".join(directives("RUN"))
    assert "chromium" in installed
    assert "chromium" in _PATH_NAMES, "the image installs a browser nothing looks for"


def test_certificates_are_installed():
    """Every call this makes is https.

    Without them Google, Cloudflare and Anthropic all fail verification with
    an error that reads like a network outage.
    """
    assert "ca-certificates" in "\n".join(directives("RUN"))


def test_the_state_directory_is_set_and_mounted():
    """A token written into the image is gone at the next deploy."""
    env = "\n".join(directives("ENV"))
    assert "OUTREACH_STATE_DIR=/data" in env
    assert any("/data" in line for line in directives("VOLUME"))


def test_it_does_not_run_as_root():
    """Writing as root makes the mounted volume root-owned, which is a
    repair job on the host rather than a setting."""
    assert directives("USER"), "no USER directive"
    assert not any(line.endswith(" root") for line in directives("USER"))


def test_the_relay_is_not_shipped():
    """Assisted send replaced it, and a Node runtime inside a Python image
    to run nothing is attack surface with no purpose."""
    assert "whatsapp-relay/" in IGNORE.read_text()
    assert "whatsapp-relay" not in "\n".join(directives("COPY"))


def test_no_secret_can_be_baked_into_a_layer():
    """A layer outlives any later deletion, and anyone who pulls the image
    can read it."""
    ignored = IGNORE.read_text()
    for secret in (".env", "credentials/", "token.json", "*.local.yaml"):
        assert secret in ignored, f"{secret} could end up in the image"


def test_the_source_is_present_before_the_install():
    """`packages.find` looks in src/.

    Installing with only pyproject.toml present succeeds and installs
    nothing -- an image whose `outreach` command does not exist, which fails
    at the moment of first use rather than at build time.
    """
    lines = DOCKERFILE.read_text().splitlines()
    copied_src = next(i for i, l in enumerate(lines) if l.startswith("COPY src/"))
    installed = next(i for i, l in enumerate(lines) if "pip install" in l)
    assert copied_src < installed, "the install runs before the source is copied"


def test_the_process_is_reaped():
    """Chromium leaves zombies behind after a screenshot times out, and PID
    1 in a container does not reap them -- a long-running dashboard would
    accumulate them until it could not fork."""
    assert any("tini" in line for line in directives("ENTRYPOINT"))


def test_the_launcher_makes_no_decision_about_who_can_reach_it():
    """`serve` binds and refuses on its own.

    A flag in the CMD is a flag someone can drop, and the last time a
    launcher decided this it failed silently on Windows for a week.
    """
    command = "\n".join(directives("CMD"))
    assert "--phone" not in command
    assert "--host" not in command
    assert "0.0.0.0" not in DOCKERFILE.read_text()


# --------------------------------------------- proved by installing it


def test_the_built_page_is_installed_with_the_package(tmp_path):
    """Not read off the repo -- actually installed, then looked for.

    Locally this never showed: the working directory is the checkout and the
    files sit beside the module either way. In a container the package is
    installed, and a missing `web/` means /app serves nothing.
    """
    if shutil.which("pip") is None and not pathlib.Path(sys.executable).exists():
        pytest.skip("no interpreter to install with")

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--no-deps", "--target", str(tmp_path), "."],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]

    page = tmp_path / "outreach" / "web" / "index.html"
    assert page.exists(), "the React page is not packaged; /app would 404"
    assert (tmp_path / "outreach" / "web" / "assets").is_dir()


def test_the_command_the_image_runs_exists(tmp_path):
    """CMD calls `outreach`. An entry point that is not declared makes that
    a container which starts, fails, and restarts forever."""
    import tomllib

    data = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    assert data["project"]["scripts"]["outreach"] == "outreach.cli:main"
    assert "outreach dashboard" in "\n".join(directives("CMD"))
