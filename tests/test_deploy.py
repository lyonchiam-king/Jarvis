"""The deploy: the health check, and what the platform file promises.

Nothing here has been deployed. `fly` is not installed in the environment
this was written in and the Docker daemon is not usable, so no machine has
ever started from this file -- what is checked is that the file and the
image agree with each other and with the app, which is where every one of
these would break silently.

The route this exists for is `/healthz`. With a password set, every other
path on the app answers a 302 to the login form, and a platform reads that
as a dead app: it restarts a perfectly healthy machine, for ever, with
nothing in any log to say why. The tests below drive the real app rather
than reading the source for a route name, because a route that exists and
is still behind the login wall passes the first kind of test and fails in
production.
"""
from __future__ import annotations

import pathlib
import tomllib
from dataclasses import replace

import pytest

from outreach.config import load_config
from outreach.dashboard import create_app

FLY = pathlib.Path("fly.toml")
DOCKERFILE = pathlib.Path("Dockerfile")

PASSWORD = "a-long-enough-password"


@pytest.fixture(scope="module")
def fly() -> dict:
    return tomllib.loads(FLY.read_text())


def guarded(**secrets):
    """The app as a server runs it: a password set, nobody signed in."""
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(
        config,
        secrets=replace(config.secrets, dashboard_password=PASSWORD, **secrets),
    )
    return create_app(config).test_client()


def dockerfile_env(name: str) -> str:
    """The value of one ENV in the image, or "" if it is not set."""
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("ENV ") and "=" in stripped:
            key, _, value = stripped[4:].partition("=")
            if key.strip() == name:
                return value.strip().strip('"')
    return ""


# --------------------------------------------------------- the health check


def test_the_health_check_answers_without_signing_in():
    assert guarded().get("/healthz").status_code == 200


def test_everything_else_is_still_behind_the_login():
    """The control. Without it the test above passes just as happily on an
    app whose password was never applied, which would prove nothing at all.
    """
    client = guarded()
    for path in ("/", "/app", "/api/summary"):
        assert client.get(path).status_code in (302, 401), path


def test_the_health_check_tells_an_unauthenticated_caller_nothing():
    """It is the one URL anybody can reach, so it may carry no fact about
    the business -- not the campaign, not which keys are set, not a count.
    """
    response = guarded(sheet_id="1SECRETSHEETID", places_api_key="AIzaSyNOTREAL")
    body = response.get("/healthz")
    assert body.get_json() == {"ok": True}

    text = body.get_data(as_text=True)
    for leak in ("1SECRETSHEETID", "AIzaSyNOTREAL", PASSWORD, "Penang", "penang"):
        assert leak not in text, f"/healthz leaked {leak!r}"


def test_the_health_check_asks_nothing_of_google(monkeypatch):
    """A check that does real work fails on a bad minute at Google, and the
    platform's answer to a failing check is to kill a process that was never
    at fault. Liveness is the question; the Sheet is not part of it.
    """
    import outreach.dashboard as dash

    def explode(*args, **kwargs):
        raise AssertionError("/healthz reached the Sheet")

    monkeypatch.setattr(dash, "_sheets_client", explode)
    monkeypatch.setattr(dash, "_load_summary", explode)
    assert guarded().get("/healthz").status_code == 200


# ------------------------------------------- the file and the app agreeing


def test_the_path_the_platform_checks_is_one_the_app_serves(fly):
    """A path typed one character wrong here is a crash loop, not a 404.

    Same shape as the endpoint check over client.ts: the file naming a URL
    and the app serving it are written in different languages and nothing
    but a test compares them.
    """
    checks = fly["http_service"]["checks"]
    assert checks, "no health check configured; the login wall becomes a restart loop"
    for check in checks:
        path = check["path"]
        assert guarded().get(path).status_code == 200, path


def test_the_port_the_platform_publishes_is_the_one_the_image_listens_on(fly):
    """Wrong here and every request is refused by the machine while the
    machine itself is perfectly fine -- which the platform reports as the
    app being unhealthy.
    """
    assert str(fly["http_service"]["internal_port"]) == dockerfile_env("PORT")
    assert fly["env"]["PORT"] == dockerfile_env("PORT")


def test_the_volume_is_mounted_where_the_image_writes(fly):
    """The one directory that survives a deploy. Mount it anywhere else and
    the Google token is written into the image's filesystem, thrown away at
    the next release, and the next sign-in needs a browser the server has
    not got.
    """
    destinations = [mount["destination"] for mount in fly["mounts"]]
    assert dockerfile_env("OUTREACH_STATE_DIR") in destinations


def test_the_deploy_builds_this_image(fly):
    assert fly["build"]["dockerfile"] == "Dockerfile"


def test_https_is_forced(fly):
    """The login form carries the password. Over plain http it crosses
    whatever network the phone is on in clear text.
    """
    assert fly["http_service"]["force_https"] is True


# ------------------------------------------------------------ no secrets


SECRET_NAMES = (
    "PASSWORD", "SECRET", "TOKEN", "API_KEY", "SHEET_ID",
    "CREDENTIALS", "ANTHROPIC", "OPENAI", "GEMINI", "GOOGLE",
)


def test_the_committed_file_carries_no_secret(fly):
    """Secrets are write-only here, and this file is committed. Every key
    goes in with `fly secrets set`, which is also the only way to change one
    without a redeploy.
    """
    for key in fly.get("env", {}):
        upper = key.upper()
        assert not any(name in upper for name in SECRET_NAMES), (
            f"{key} belongs in `fly secrets set`, not in a committed file"
        )


def test_the_daily_run_is_off_unless_asked_for(fly):
    """A scheduled run spends money on Places lookups, research, a brief and
    a message per lead. Nobody should discover that from a bill because a
    file they copied had it switched on.
    """
    assert "OUTREACH_DAILY_RUN" not in fly.get("env", {})


def test_a_sleeping_machine_is_flagged_beside_the_schedule(fly):
    """Auto-stop and the morning batch contradict each other: a stopped
    machine runs no threads, so the run simply never happens -- and it looks
    like a scheduler bug rather than a machine that was asleep. The file may
    keep auto-stop, but it has to say so where the setting is.
    """
    if fly["http_service"].get("auto_stop_machines") in (None, False, "off"):
        return
    text = FLY.read_text()
    assert "OUTREACH_DAILY_RUN" in text and "min_machines_running = 1" in text, (
        "auto-stop is on with nothing warning that it disables the daily run"
    )
