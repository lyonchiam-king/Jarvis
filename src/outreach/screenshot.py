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


def source_uri(source) -> str:
    """A file path or an http(s) URL, as something a browser can open.

    Both are needed. A site generated here sits on disk, but one built in
    Google AI Studio or Lovable exists only at a URL -- and photographing
    only the local file meant every externally built site silently got no
    screenshot at all.
    """
    text = str(source or "").strip()
    if not text:
        # Path("") is Path("."), which exists -- so an empty source would
        # otherwise resolve to the working directory and photograph a file
        # listing instead of failing.
        return ""
    if text.lower().startswith(("http://", "https://")):
        return text
    path = Path(text)
    if not path.exists():
        return ""
    return path.resolve().as_uri()



# Chrome writes a lot of harmless noise to stderr. Reporting the last line
# verbatim put "Every renderer should have at least one task provided by a
# primary provider" in front of Lyon as though it were the cause, which it
# is not -- it is a warning every headless run emits.
_NOISE = (
    "fallback_task_provider",
    "Every renderer should have",
    "GPU process",
    "dbus",
    "Fontconfig",
    "DEPRECATED_ENDPOINT",
    "voice_transcription",
)


def _why(result) -> str:
    """The most useful line of a browser's stderr, or a plain shrug."""
    raw = (getattr(result, "stderr", b"") or b"")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not any(n in line for n in _NOISE)
    ]
    return lines[-1][:200] if lines else "no image and nothing explaining why"


def capture(
    page,
    out: Path,
    *,
    browser: str = "",
    width: int = WIDTH,
    height: int = HEIGHT,
    timeout: float = 60.0,
    runner=subprocess.run,
) -> Shot:
    """Screenshot ``page`` into ``out``. Raises ScreenshotError on failure."""
    uri = source_uri(page)
    if not uri:
        raise ScreenshotError(f"No page to photograph at {page}")

    exe = find_browser(browser)
    if not exe:
        raise ScreenshotError(
            "No browser found to take the screenshot. Edge or Chrome will do "
            "-- set build.screenshot_browser to its full path if it is "
            "installed somewhere unusual."
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    # A remote page needs time to load before it is worth photographing;
    # a local file is painted immediately.
    delay = ["--virtual-time-budget=6000"] if uri.startswith("http") else []

    # Chrome's headless modes disagree about --screenshot, and which one
    # works depends on the build sitting on the machine. "new" is the modern
    # default and silently wrote nothing on Lyon's Edge; "old" is the
    # long-standing path for --screenshot; the bare flag is what older
    # builds understand. Trying them in turn costs a second on the machines
    # where the first works and is the difference between a feature and a
    # confusing error everywhere else.
    modes = (["--headless=new"], ["--headless=old"], ["--headless"])

    attempts: list[str] = []
    for mode in modes:
        with tempfile.TemporaryDirectory(prefix="jarvis-shot-") as profile:
            cmd = [
                exe,
                *mode,
                "--disable-gpu",
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                *delay,
                f"--screenshot={out}",
                uri,
            ]
            try:
                result = runner(cmd, capture_output=True, timeout=timeout, check=False)
            except subprocess.TimeoutExpired:
                # No retry. A timeout means the browser started and the page
                # never settled, which no other headless flag fixes -- and
                # trying all three would cost three times the budget per
                # lead and make a run look frozen, which is the thing the
                # short timeout exists to prevent.
                raise ScreenshotError(
                    f"The browser did not finish within {timeout:.0f}s."
                )
            except OSError as exc:
                # The browser could not be run at all, which no other mode
                # will fix.
                raise ScreenshotError(f"Could not run {exe}: {exc}")

        # The exit code is not proof: headless Chrome reports success and
        # writes nothing often enough that only bytes on disk count.
        if out.exists() and out.stat().st_size:
            return Shot(path=out, width=width, height=height)

        # Some builds ignore the path and drop the file in the working
        # directory instead. Claim it rather than calling this a failure.
        stray = Path.cwd() / "screenshot.png"
        if stray.exists() and stray.stat().st_size and stray != out:
            stray.replace(out)
            return Shot(path=out, width=width, height=height)

        attempts.append(f"{mode[0]}: {_why(result)}")

    raise ScreenshotError(
        "The browser wrote no image in any headless mode. Tried -- "
        + "; ".join(attempts)
    )


def shot_path_for(page: Path) -> Path:
    """Where a page's screenshot belongs -- beside it, deterministically.

    No schema column: the site directory is already derived from the lead's
    slug, so the picture can always be found again without storing a path
    that could go stale.
    """
    return Path(page).parent / "screenshot.png"
