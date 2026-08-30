"""Suite-wide guards against a test damaging the machine it runs on.

The dashboard writes real files -- `.env` and the campaign's `*.local.yaml`
-- because that is its job. A test that exercises those paths without first
moving to a temp directory writes them for real, in the repository.

That is not hypothetical. A run of this suite left CAMPAIGN_CONFIG in the
developer's `.env` pointing at a pytest temp directory, and because every
command loads config first, *all* of them then failed with "Campaign config
not found" -- drafting included. The damage outlived the test run and was
invisible until something unrelated broke.
"""

import hashlib
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Files the suite must never modify in place. Content is hashed rather than
# read into an assertion: .env holds live API keys and must not reach a test
# report, a log, or a diff.
PROTECTED = (REPO / ".env", REPO / "config" / "campaigns" / "penang-my.local.yaml")


def _fingerprint(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@pytest.fixture(autouse=True)
def _protect_real_config_files():
    """Fail the test that writes a real config file, naming it.

    Autouse and per-test, so the failure lands on the test that caused it
    rather than on whatever runs next.
    """
    before = {path: _fingerprint(path) for path in PROTECTED}
    yield
    for path, was in before.items():
        now = _fingerprint(path)
        if now != was:
            what = "created" if was is None else "modified"
            pytest.fail(
                f"This test {what} {path}, a real file outside the test "
                "sandbox. Use monkeypatch.chdir(tmp_path) before exercising "
                "anything that saves settings -- the dashboard writes .env "
                "and the campaign's .local.yaml relative to the working "
                "directory, and a stale CAMPAIGN_CONFIG breaks every command."
            )


@pytest.fixture(autouse=True)
def _no_leaked_campaign_config():
    """Stop one test's CAMPAIGN_CONFIG leaking into the next.

    The save path sets os.environ as well as writing .env, and an env var
    survives monkeypatch.chdir -- so without this a later test loads a
    campaign from a temp directory that has already been cleaned up.
    """
    before = os.environ.get("CAMPAIGN_CONFIG")
    yield
    after = os.environ.get("CAMPAIGN_CONFIG")
    if after != before:
        if before is None:
            os.environ.pop("CAMPAIGN_CONFIG", None)
        else:
            os.environ["CAMPAIGN_CONFIG"] = before
