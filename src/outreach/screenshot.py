"""A picture of the built site, for attaching to the outreach message.

Deliberately no new dependency. Lyon runs Windows and is not a programmer,
so "pip install playwright" plus a 150MB browser download is a real cost to
him and a real thing to go wrong. Every Windows machine already has Edge,
most have Chrome, and both take a headless screenshot from the command line
-- so this drives whichever it finds rather than installing a browser of its
own.

The page is captured from the local file, not a URL: the generated sites
live on disk next to index.html and there is no hosting step yet, so the
file is the only thing that reliably exists.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("outreach")

# Where the browsers actually are, in the order worth trying. Edge first
# because it is present on every Windows install without anyone choosing it.
_WINDOWS_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

# Names to look for on PATH, which covers Linux, macOS, and a Windows box
# where a browser was installed somewhere unusual.
_PATH_NAMES = (
    "msedge",
    "microsoft-edge",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
)

# A phone-ish viewport. The site is designed mobile-first and the recipient
# opens the picture on a phone, so a wide desktop shot would show a layout
# they will never see, with the hero shrunk to nothing.
WIDTH = 900
HEIGHT = 1200


class ScreenshotError(Exception):
    """No picture could be taken. Never fatal -- the message still sends."""


@dataclass(frozen=True)
class Shot:
    path: Path
    width: int
    height: int


def find_browser(explicit: str = "") -> str:
    """The browser to drive, or "" when there is none.

    ``explicit`` wins when set, so a machine with a browser somewhere odd is
    a config line rather than a code change.
    """
    if explicit:
        return explicit if Path(explicit).exists() else ""

    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for candidate in _WINDOWS_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def capture(
    page: Path,
    out: Path,
    *,
    browser: str = "",
    width: int = WIDTH,
    height: int = HEIGHT,
    timeout: float = 60.0,
    runner=subprocess.run,
) -> Shot:
    """Screenshot ``page`` into ``out``. Raises ScreenshotError on failure."""
    page = Path(page)
    if not page.exists():
        raise ScreenshotError(f"No page to photograph at {page}")

    exe = find_browser(browser)
    if not exe:
        raise ScreenshotError(
            "No browser found to take the screenshot. Edge or Chrome will do "
            "-- set build.screenshot_browser to its full path if it is "
            "installed somewhere unusual."
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    # A throwaway profile: without one, a browser already running as the
    # user's own window refuses to start a second instance and the call
    # returns success having done nothing.
    with tempfile.TemporaryDirectory(prefix="jarvis-shot-") as profile:
        cmd = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            f"--screenshot={out}",
            page.resolve().as_uri(),
        ]
        try:
            result = runner(
                cmd, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            raise ScreenshotError(
                f"The browser did not finish within {timeout:.0f}s."
            )
        except OSError as exc:
            raise ScreenshotError(f"Could not run {exe}: {exc}")

    # The exit code is not proof: headless Chrome has been known to report
    # success and write nothing, so what matters is a file with bytes in it.
    if not out.exists() or out.stat().st_size == 0:
        detail = (getattr(result, "stderr", b"") or b"")[:300].decode(
            "utf-8", "replace"
        )
        raise ScreenshotError(
            f"The browser wrote no image{': ' + detail if detail.strip() else '.'}"
        )

    return Shot(path=out, width=width, height=height)


def shot_path_for(page: Path) -> Path:
    """Where a page's screenshot belongs -- beside it, deterministically.

    No schema column: the site directory is already derived from the lead's
    slug, so the picture can always be found again without storing a path
    that could go stale.
    """
    return Path(page).parent / "screenshot.png"
