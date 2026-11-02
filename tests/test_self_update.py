"""The launcher's update step, driven against real git repositories.

Mocking git here would prove almost nothing: what has to be true is that a
fast-forward happens on an ordinary checkout, that a local edit survives, and
that the working copy's own litter -- logs, caches, built sites -- does not
count as a local edit and silently switch updating off for ever. Those are
facts about git's behaviour, so the tests use git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from outreach import paths, selfupdate


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(list(args), cwd=str(cwd), check=True, capture_output=True, text=True)


def _identify(repo: Path) -> None:
    # A container has no global git identity, so a commit would refuse.
    _run(repo, "git", "config", "user.email", "test@example.com")
    _run(repo, "git", "config", "user.name", "Test")


def _commit(repo: Path, name: str, body: str, message: str) -> None:
    (repo / name).write_text(body)
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-qm", message)


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    """An origin with one commit, and a clone of it.

    Commits are made directly in origin rather than pushed, because a
    non-bare repository refuses a push to the branch it has checked out --
    and fetching is the only half of it under test.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _run(origin, "git", "init", "-q", "-b", "main")
    _identify(origin)
    _commit(origin, "app.py", "first\n", "first")

    clone = tmp_path / "clone"
    _run(tmp_path, "git", "clone", "-q", str(origin), str(clone))
    _identify(clone)
    return origin, clone


def test_a_pushed_fix_is_fetched_and_applied(pair):
    """The whole point. Three fixes were pushed and never arrived."""
    origin, clone = pair
    _commit(origin, "app.py", "the fix\n", "fix: the button")

    result = selfupdate.update(clone)

    assert result.outcome == "updated"
    assert result.commits == 1
    assert (clone / "app.py").read_text() == "the fix\n"
    assert result.before != result.after
    # Phrased for someone who is not a programmer and did not ask for this.
    assert "git" not in result.message.lower()


def test_two_pushes_are_reported_as_two(pair):
    origin, clone = pair
    _commit(origin, "app.py", "one\n", "first fix")
    _commit(origin, "app.py", "two\n", "second fix")

    result = selfupdate.update(clone)

    assert result.outcome == "updated"
    assert result.commits == 2
    assert "2 updates" in result.message


def test_being_current_is_not_reported_as_work_done(pair):
    _, clone = pair

    result = selfupdate.update(clone)

    assert result.outcome == "current"
    assert result.commits == 0


def test_a_local_edit_is_never_discarded(pair):
    """Gaining a newer dashboard is not worth silently destroying someone's
    change. The update is skipped and the edit is still there afterwards."""
    origin, clone = pair
    (clone / "app.py").write_text("my own edit\n")
    _commit(origin, "app.py", "the fix\n", "fix: something")

    result = selfupdate.update(clone)

    assert result.outcome == "skipped"
    assert "your own changes" in result.message
    assert (clone / "app.py").read_text() == "my own edit\n"


def test_untracked_files_do_not_block_an_update(pair):
    """The control on the local-edit check.

    `installer-log.txt`, `relay-log.txt`, `sites/` and `.cache/` all live in
    the working copy. A plain `git status --porcelain` lists them, so reading
    that as "local edits" would skip every update for ever on exactly the
    machine that has been running for a while -- the one most in need of a
    fix. Only tracked modifications may block a fast-forward.
    """
    origin, clone = pair
    (clone / "installer-log.txt").write_text("noise\n")
    (clone / "relay-log.txt").write_text("more noise\n")
    (clone / "sites").mkdir()
    (clone / "sites" / "index.html").write_text("<html></html>")
    _commit(origin, "app.py", "the fix\n", "fix: something")

    result = selfupdate.update(clone)

    assert result.outcome == "updated"
    assert (clone / "app.py").read_text() == "the fix\n"
    # And the litter is still where it was.
    assert (clone / "installer-log.txt").exists()


def test_a_divergent_branch_is_never_merged(pair):
    """`--ff-only`. A conflict cannot be resolved on an unattended machine
    whose owner is not a programmer, and a half-merged checkout is worse
    than an old one."""
    origin, clone = pair
    _commit(clone, "mine.py", "local work\n", "local commit")
    _commit(origin, "app.py", "theirs\n", "their commit")

    result = selfupdate.update(clone)

    assert result.outcome == "skipped"
    assert "could not be applied" in result.message
    assert (clone / "app.py").read_text() == "first\n"
    assert (clone / "mine.py").exists()


def test_being_offline_is_not_a_failure_that_stops_anything(pair, tmp_path):
    """Opening the dashboard on a train must give you the dashboard."""
    origin, clone = pair
    _run(clone, "git", "remote", "set-url", "origin", str(tmp_path / "gone"))

    result = selfupdate.update(clone)

    assert result.outcome == "skipped"
    assert "Could not reach GitHub" in result.message


def test_a_hosted_copy_is_left_to_its_deploy(monkeypatch, pair):
    """A server's copy is read-only and replaced every release, so pulling
    into it either fails or is thrown away at the next deploy."""
    _, clone = pair
    monkeypatch.setenv(paths.ENV_VAR, str(clone))

    result = selfupdate.update(clone)

    assert result.outcome == "skipped"
    assert "deploy" in result.message


def test_git_not_installed_names_the_one_fix(monkeypatch, pair):
    """A real Windows case: the project downloaded as a zip, with no git."""
    _, clone = pair

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)

    result = selfupdate.update(clone)

    assert result.outcome == "skipped"
    assert "git-scm.com" in result.message


def test_a_folder_that_is_not_a_checkout_says_so(tmp_path, monkeypatch):
    plain = tmp_path / "zip-download"
    plain.mkdir()
    monkeypatch.setattr(selfupdate, "repo_root", lambda *a, **k: None)

    result = selfupdate.update()

    assert result.outcome == "skipped"
    assert "not downloaded with Git" in result.message


def test_update_never_raises_whatever_git_does(monkeypatch, pair):
    """The launcher runs this before opening the dashboard. An exception
    here would stop the tool starting over a step that is only ever a
    convenience."""
    _, clone = pair

    def exploding(*args, **kwargs):
        raise OSError("something no one predicted")

    monkeypatch.setattr(subprocess, "run", exploding)

    result = selfupdate.update(clone)

    assert result.outcome == "skipped"


def test_describe_names_the_commit_actually_on_disk(pair):
    _, clone = pair
    expected = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(clone),
        capture_output=True,
        text=True,
    ).stdout.strip()

    version = selfupdate.describe(clone)

    assert version.commit == expected
    assert version.branch == "main"
    # A plain YYYY-MM-DD, read to compare against "pushed on the 2nd".
    assert len(version.date) == 10 and version.date.count("-") == 2
    assert version.label.startswith(expected)


def test_an_unknown_build_is_blank_rather_than_invented(monkeypatch):
    """A made-up version string is the invented-fact class the front end is
    checked for, and this one would be invented on the exact screen somebody
    reads to decide whether a fix has arrived."""
    monkeypatch.setattr(selfupdate, "repo_root", lambda *a, **k: None)

    version = selfupdate.describe()

    assert version.commit == ""
    assert version.label == "unknown build"


def test_repo_root_finds_the_checkout_from_the_module_not_the_cwd(tmp_path, monkeypatch):
    """The dashboard and the tests both change directory, which is why
    `state_dir()` is never captured at import. Walking up from the module
    makes this one immune instead."""
    monkeypatch.chdir(tmp_path)

    root = selfupdate.repo_root()

    assert root is not None
    assert (root / "src" / "outreach" / "selfupdate.py").exists()
