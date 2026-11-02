"""Bringing a pushed fix to the machine that needs it.

A fix that cannot reach the person who reported the bug does not exist.
`Dashboard.bat` printed "Checking for updates..." and then ran
`pip install -e .`, which reinstalls whatever is already on disk -- so three
fixes in a row were written, tested, pushed, and reported back as still
broken: "it still doesn't show any button", "there's no button to send",
"still not showing any of the drop sent to queue button. Are you sure you
fixed it?". Each report was correct. The code was right and the machine had
never received it, because the only way to receive it was a `git pull` typed
into a terminal by someone who does not use one.

So the launcher fetches. Everything about how that is decided lives here
rather than in the batch file, which is the same line the `--phone` flag
sits on: anything that must be right on Windows and cannot be run from this
container belongs on the Python side, where a test can drive it.

The hard rule is that **this is never allowed to stop the dashboard
opening**. Being a day behind is an inconvenience; a launcher that refuses
to start because GitHub was unreachable, or because git is not installed, or
because the checkout has a local edit, is the tool being broken by its own
update step. Every failure here is a sentence on the way past.

Three things are deliberately not done:

- **No merge, ever.** `--ff-only`, so the update is either a straight
  fast-forward or it is skipped. Resolving a conflict is not something that
  can happen unattended on a machine whose owner is not a programmer, and a
  half-merged checkout is far worse than an old one.
- **Nothing is stashed or discarded.** A tracked file with local edits means
  someone changed something; throwing that away to gain a newer dashboard is
  not a trade this code is entitled to make on their behalf.
- **Untracked files are ignored.** `installer-log.txt`, `relay-log.txt`,
  `sites/`, `.cache/` and the visuals folder all live in the working copy
  and none of them has any bearing on whether a fast-forward is safe.
  Counting them as "local edits" would skip every update for ever.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import paths

# Generous enough for a slow fetch over a phone hotspot, short enough that a
# hung git cannot hold the dashboard shut. Each call gets its own budget.
GIT_TIMEOUT = 45.0


@dataclass(frozen=True)
class UpdateResult:
    """What happened, and the one line to print about it.

    `outcome` is for tests and callers; `message` is for the person reading
    the launcher window, so it says what it means without naming git.
    """

    outcome: str  # updated | current | skipped
    message: str
    before: str = ""
    after: str = ""
    commits: int = 0


@dataclass(frozen=True)
class Version:
    """Which build this is, in a form somebody can read off a screenshot."""

    commit: str = ""
    date: str = ""
    branch: str = ""

    @property
    def label(self) -> str:
        if not self.commit:
            return "unknown build"
        if self.date:
            return f"{self.commit} · {self.date}"
        return self.commit


def _git(root: Path, *args: str) -> tuple[int, str]:
    """Run one git command. Returns its exit code and its combined output.

    Never raises. git missing, git hanging and git refusing all have to come
    back as an ordinary failure, because the caller's job is to carry on.
    """
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError:
        return 127, "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "git did not finish in time"
    except OSError as exc:  # pragma: no cover - platform specific
        return 1, str(exc)
    out = (done.stdout or "") + (done.stderr or "")
    return done.returncode, out.strip()


def repo_root(start: Path | None = None) -> Path | None:
    """The checkout this package is running from, or None if it is not one.

    Walks up from the module rather than from the working directory: the
    dashboard and the tests both change directory, which is the same reason
    `state_dir()` is read on every call instead of captured at import.
    """
    here = (start or Path(__file__).resolve()).resolve()
    for folder in [here, *here.parents]:
        if (folder / ".git").exists():
            return folder
    return None


def describe(root: Path | None = None) -> Version:
    """Which commit is on disk, for the stamp the dashboard shows.

    An unknown answer is returned as blank rather than guessed at. A made-up
    version string is exactly the class of invented fact the front end is
    checked for, and it would be invented on the one screen somebody reads
    to decide whether a fix has arrived.
    """
    root = root or repo_root()
    if root is None:
        return Version()
    code, commit = _git(root, "rev-parse", "--short", "HEAD")
    if code != 0:
        return Version()
    # %cs is the committer date as plain YYYY-MM-DD. Not localised: this is
    # read to compare against "the fix was pushed on the 2nd", not to tell
    # the time.
    _, date = _git(root, "log", "-1", "--format=%cs")
    _, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return Version(
        commit=commit.strip(),
        date=date.strip() if date else "",
        branch=branch.strip() if branch else "",
    )


def update(root: Path | None = None) -> UpdateResult:
    """Fast-forward this checkout onto its branch, or explain why not."""
    if paths.is_hosted():
        # A server's copy of the code is read-only and replaced every
        # deploy, so pulling into it either fails or is thrown away at the
        # next release. The deploy is the update there.
        return UpdateResult(
            "skipped",
            "This copy is managed by the server, so it updates when you deploy.",
        )

    root = root or repo_root()
    if root is None:
        return UpdateResult(
            "skipped",
            "This copy was not downloaded with Git, so it cannot update itself.",
        )

    code, out = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code == 127:
        return UpdateResult(
            "skipped",
            "Git is not installed, so updates cannot be fetched. Install it "
            "from git-scm.com and this will start working by itself.",
        )
    if code != 0:
        return UpdateResult("skipped", f"Could not check for updates: {_first_line(out)}")
    branch = out.strip()
    if not branch or branch == "HEAD":
        return UpdateResult(
            "skipped",
            "This copy is not on a branch, so it was left exactly as it is.",
        )

    # Tracked modifications only. Untracked files are the logs, the built
    # sites and the caches, and none of them blocks a fast-forward.
    code, dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if code != 0:
        return UpdateResult("skipped", f"Could not check for updates: {_first_line(dirty)}")
    if dirty.strip():
        return UpdateResult(
            "skipped",
            "You have your own changes in this folder, so the update was "
            "skipped and nothing was touched.",
        )

    before = describe(root).commit

    code, out = _git(root, "fetch", "--quiet", "origin", branch)
    if code != 0:
        # Offline is the ordinary case here, not a fault. Someone opening
        # the dashboard on a train should get the dashboard.
        return UpdateResult(
            "skipped",
            "Could not reach GitHub to check for updates, so this is "
            "running the copy you already have.",
            before=before,
        )

    code, out = _git(root, "rev-list", "--count", f"HEAD..origin/{branch}")
    if code != 0:
        return UpdateResult("skipped", f"Could not check for updates: {_first_line(out)}", before=before)
    try:
        behind = int(out.strip() or "0")
    except ValueError:
        behind = 0
    if behind == 0:
        return UpdateResult("current", "Already up to date.", before=before, after=before)

    code, out = _git(root, "merge", "--ff-only", f"origin/{branch}")
    if code != 0:
        return UpdateResult(
            "skipped",
            "There is an update, but it could not be applied automatically. "
            "Nothing was changed.",
            before=before,
            commits=behind,
        )

    after = describe(root).commit
    fixes = "1 update" if behind == 1 else f"{behind} updates"
    return UpdateResult(
        "updated",
        f"Updated: {fixes} downloaded.",
        before=before,
        after=after,
        commits=behind,
    )


def _first_line(text: str) -> str:
    """git's first line only.

    Its later lines are advice aimed at a developer -- "use --allow-unrelated
    histories", a block about setting an upstream -- which on this screen is
    noise in front of a person who cannot act on it.
    """
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return "no details"


_CACHED: Version | None = None


def cached_describe() -> Version:
    """`describe()`, computed once per process.

    The dashboard polls its summary every thirty seconds and the stamp rides
    along on it, so asking git three times a minute for an answer that cannot
    change is pure waste: the update runs in the launcher, before this
    process starts, so the commit on disk is fixed for as long as it lives.

    Safe to capture where `state_dir()` deliberately is not, because
    `repo_root` walks up from this file rather than from the working
    directory -- the thing that moves under the dashboard and the tests.
    """
    global _CACHED
    if _CACHED is None:
        _CACHED = describe()
    return _CACHED
