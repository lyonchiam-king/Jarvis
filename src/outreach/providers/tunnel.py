"""A public address for the dashboard, via Cloudflare's `cloudflared`.

Four separate things stopped a phone reaching this dashboard over wifi: the
machine's address (wrong whenever a VPN or a virtual adapter owns the
internet route), Windows Firewall, whatever the router does to traffic
between devices, and browsers refusing a plain-http address. A tunnel
removes all four at once, because the connection is made *outbound* from
this machine and the phone talks to Cloudflare over https.

What it costs is the thing worth saying plainly: the dashboard stops being
reachable only from the house and becomes reachable from the internet, with
the password as the only thing in front of it. `serve` refuses to open one
without a strong password for that reason -- see `dashboard.serve`.

This uses a **quick tunnel**, which needs no Cloudflare account, no domain
and no login. The address it hands back is random and changes every time the
tunnel starts. That is the trade for needing no setup, and it means a
home-screen icon has to be remade when the address changes.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# What cloudflared prints once the tunnel is up. It goes to stderr, mixed in
# with a banner, so it is matched rather than positioned.
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# How long to wait for that line before giving up. A tunnel that has not
# announced itself in this long is not slow, it is failing -- and waiting
# longer only delays saying so.
STARTUP_TIMEOUT = 30.0

# Cloudflare's own release channel. Over https, so the connection is
# authenticated to github.com, which is where Cloudflare publishes -- the
# same trust as downloading it by hand, which is the alternative.
DOWNLOADS = {
    ("Windows", "AMD64"): "cloudflared-windows-amd64.exe",
    ("Windows", "ARM64"): "cloudflared-windows-arm64.exe",
    ("Darwin", "arm64"): "cloudflared-darwin-arm64.tgz",
    ("Darwin", "x86_64"): "cloudflared-darwin-amd64.tgz",
    ("Linux", "x86_64"): "cloudflared-linux-amd64",
    ("Linux", "aarch64"): "cloudflared-linux-arm64",
}
RELEASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/"


def download_name() -> str | None:
    """The release asset for this machine, or None if it is not published."""
    import platform

    return DOWNLOADS.get((platform.system(), platform.machine()))


def fetch_cloudflared(root: Path | None = None, *, timeout: float = 300.0) -> Path:
    """Download cloudflared beside the project, and return where it landed.

    Installing a program is the one step left in "put the dashboard on my
    phone", and it is exactly the kind of step that stalls someone who is not
    a programmer -- find the right file among six, know which architecture
    you have, put it somewhere the tool will look. So this does it.

    It fetches Cloudflare's own published build over https. That is the same
    thing a person does by hand; nothing here makes it safer than that, and
    nothing makes it less safe either. There is no checksum to pin because
    the release moves: a hash written here would be wrong by the next
    version and would refuse a perfectly good download.
    """
    import platform

    root = root or Path.cwd()
    asset = download_name()
    if asset is None:
        raise TunnelError(
            f"No published cloudflared for {platform.system()} "
            f"{platform.machine()}.\n\n{WHERE_TO_GET_IT}"
        )
    if asset.endswith(".tgz"):
        raise TunnelError(
            "On this machine cloudflared ships as an archive rather than a "
            f"single file, so it needs unpacking by hand.\n\n{WHERE_TO_GET_IT}"
        )

    import requests

    target = root / ("cloudflared.exe" if asset.endswith(".exe") else "cloudflared")
    # Written under a temporary name and moved into place, so an interrupted
    # download cannot leave a half-file that looks installed and fails oddly
    # on every later run.
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with requests.get(RELEASE_URL + asset, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    handle.write(chunk)
    except Exception as exc:  # noqa: BLE001 -- reported, never raised raw
        partial.unlink(missing_ok=True)
        raise TunnelError(
            f"Could not download cloudflared: {exc}\n\n{WHERE_TO_GET_IT}"
        ) from exc

    if partial.stat().st_size < 1_000_000:
        # A few hundred bytes is an error page, not a program. Running it
        # would fail with something unrelated to the real problem.
        size = partial.stat().st_size
        partial.unlink(missing_ok=True)
        raise TunnelError(
            f"The download was only {size} bytes, which is not the program.\n"
            f"Something on this network may be intercepting it.\n\n{WHERE_TO_GET_IT}"
        )

    partial.replace(target)
    if not asset.endswith(".exe"):
        target.chmod(target.stat().st_mode | 0o111)
    return target


WHERE_TO_GET_IT = (
    "cloudflared is not installed.\n\n"
    "Download the Windows .exe from Cloudflare:\n"
    "    https://developers.cloudflare.com/cloudflare-one/connections/"
    "connect-networks/downloads/\n\n"
    "Save it as cloudflared.exe next to Dashboard.bat, or install it with\n"
    "winget and it will be found automatically:\n"
    "    winget install --id Cloudflare.cloudflared"
)


class TunnelError(Exception):
    """The tunnel could not be started, with a reason worth reading."""


def find_cloudflared(root: Path | None = None) -> Path | None:
    """The cloudflared binary, or None.

    Looks beside the project first so that dropping the .exe in the folder is
    enough -- a non-programmer should not have to edit PATH to use this.
    """
    root = root or Path.cwd()
    for name in ("cloudflared.exe", "cloudflared"):
        local = root / name
        if local.exists():
            return local
    found = shutil.which("cloudflared")
    return Path(found) if found else None


class Tunnel:
    """A running quick tunnel, and the address it published.

    Started and stopped by `serve`. Nothing else keeps a reference, so the
    process cannot outlive the dashboard that opened it -- a tunnel left
    running after the window closed would leave the machine published with
    nobody aware of it.
    """

    def __init__(self, port: int, *, binary: Path | None = None, root: Path | None = None):
        self.port = port
        self.root = root
        self.binary = binary or find_cloudflared(root)
        self.process: subprocess.Popen | None = None
        self.public_url: str = ""
        self.error: str = ""

    def ensure_binary(self, *, download: bool = True) -> Path:
        """The binary, fetching it once if it is not here yet.

        Called before the tunnel opens rather than at the point of failure,
        so the download is a line of output on the way in rather than an
        error after a wait.
        """
        if self.binary is not None:
            return self.binary
        if not download:
            raise TunnelError(WHERE_TO_GET_IT)
        self.binary = fetch_cloudflared(self.root)
        return self.binary

    def start(self) -> str:
        """Open the tunnel and return its public https address.

        Raises TunnelError with something actionable rather than returning an
        empty string: a caller that got "" would have to guess whether the
        binary was missing, the network was down, or Cloudflare said no.
        """
        if self.binary is None:
            raise TunnelError(WHERE_TO_GET_IT)

        try:
            self.process = subprocess.Popen(
                [
                    str(self.binary), "tunnel",
                    "--url", f"http://127.0.0.1:{self.port}",
                    # Updating itself mid-run would restart the tunnel and
                    # change the address underneath a phone that is using it.
                    "--no-autoupdate",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise TunnelError(f"Could not start cloudflared: {exc}") from exc

        found = threading.Event()

        def read_output() -> None:
            # cloudflared keeps writing for as long as it runs, so this
            # thread outlives the wait below and keeps the pipe drained --
            # a full pipe would block the tunnel itself.
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                if not self.public_url:
                    match = URL_PATTERN.search(line)
                    if match:
                        self.public_url = match.group(0)
                        found.set()
                log.debug("cloudflared: %s", line.rstrip())
            # The pipe closing means the process ended. If that happened
            # before an address appeared, unblock the wait rather than
            # letting it run out the full timeout for no reason.
            found.set()

        threading.Thread(target=read_output, daemon=True).start()
        found.wait(STARTUP_TIMEOUT)

        if not self.public_url:
            code = self.process.poll()
            self.stop()
            if code is not None:
                raise TunnelError(
                    f"cloudflared stopped straight away (exit {code}).\n"
                    "Usually that is no internet connection, or a firewall "
                    "blocking outbound connections."
                )
            raise TunnelError(
                f"cloudflared did not publish an address within "
                f"{STARTUP_TIMEOUT:.0f} seconds. Check this machine can reach "
                "the internet."
            )
        return self.public_url

    def stop(self) -> None:
        """End the tunnel. Safe to call twice, and never raises.

        Called from a finally block while the dashboard is shutting down, so
        a throw here would replace whatever actually went wrong.
        """
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:  # noqa: BLE001 -- shutdown must not fail
            try:
                self.process.kill()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.process = None
