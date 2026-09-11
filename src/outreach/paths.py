"""Where everything this tool writes is allowed to live.

On Lyon's machine the answer is "beside the code", and always has been: the
repo is his working directory, `.env` sits in it, `credentials/` beside that,
`.cache/` for scratch. That is right for a tool you start by double-clicking
a file in the folder.

It is wrong for a server. A container's copy of the code is read-only and
replaced on every deploy, so anything written into it is either refused or
silently thrown away at the next release -- and the two things that would be
thrown away here are the Google token and every key the dashboard has saved.

So one directory owns all of it, named by `OUTREACH_STATE_DIR`, and the
default is the current working directory -- which reproduces today's layout
exactly. Nothing changes for anyone who does not set the variable.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "OUTREACH_STATE_DIR"


def state_dir() -> Path:
    """The one writable directory. Defaults to today's behaviour.

    Read on every call rather than captured at import: the dashboard and the
    tests both change the working directory, and a module-level constant
    would freeze whichever came first.
    """
    configured = os.getenv(ENV_VAR, "").strip()
    return Path(configured) if configured else Path.cwd()


def env_file() -> Path:
    """The .env the dashboard writes keys into.

    Note this is where keys are *written*, not where they are read from: a
    hosted deployment passes them as real environment variables and never
    needs this file at all. It exists so that saving a key from the page
    works in both places.
    """
    return state_dir() / ".env"


def credentials_dir() -> Path:
    """Google's OAuth client and the token it issued.

    The token is the single most valuable thing this tool stores and the one
    a deploy must not erase -- losing it means signing in again with a
    browser, which a server has no way to do.
    """
    return state_dir() / "credentials"


def cache_dir() -> Path:
    """Scratch space -- screenshots, probe output. Safe to lose."""
    return state_dir() / ".cache"


def campaign_dir() -> Path:
    """Where an edited campaign is kept.

    Local runs keep the twin beside the tracked template, which is what the
    gitignore expects and what every existing install already has. A server
    has no writable `config/` and must put it in the state directory, so the
    fork asks here rather than assuming.
    """
    return state_dir() / "campaigns"


def is_hosted() -> bool:
    """Whether state has been deliberately moved out of the working copy.

    Not a guess about the environment -- containers are not detectable, and
    trying makes behaviour depend on something nobody set. It is simply
    whether someone answered the question.
    """
    return bool(os.getenv(ENV_VAR, "").strip())
