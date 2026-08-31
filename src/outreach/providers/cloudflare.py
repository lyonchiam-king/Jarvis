"""Publishing a built site to Cloudflare Pages.

The sites are built in Google AI Studio and pushed from there into a GitHub
repository. Jarvis reads them out of a local clone of that repo and uploads
them to Cloudflare directly, rather than connecting Pages to GitHub: a
direct upload returns a URL in seconds and has one moving part, where the
Pages/GitHub integration adds a build wait and a second place to look when
nothing appears.

Nothing here has been run against the real API -- this container has no
outbound network -- so `outreach cloudflare-check` prints whatever comes
back rather than assuming. The same discipline as the model names: ask the
API, do not trust a shape written from memory.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger("outreach")

API = "https://api.cloudflare.com/client/v4"

# What a site is allowed to be made of. A publish should carry a page and
# its pictures, not a repository's worth of source, licences and lockfiles.
PUBLISHABLE = frozenset(
    {".html", ".htm", ".css", ".js", ".png", ".jpg", ".jpeg", ".webp",
     ".svg", ".gif", ".ico", ".woff", ".woff2", ".txt", ".json"}
)

# Cloudflare's own limit is 25MiB per file. A generated page and its images
# are far below this; anything above it is a mistake worth naming.
MAX_FILE_BYTES = 25 * 1024 * 1024


class CloudflareError(Exception):
    """The publish did not happen. Always says what to do next."""


@dataclass(frozen=True)
class Deployment:
    url: str
    deployment_id: str
    project: str


def site_files(folder: Path) -> list[Path]:
    """Every publishable file under ``folder``, deepest paths included.

    Dotfiles and anything outside PUBLISHABLE are skipped: a repo exported
    from AI Studio carries a .git directory and assorted config that has no
    business on a prospect's website.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise CloudflareError(f"No such folder to publish: {folder}")

    out = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(folder).parts):
            continue
        if path.suffix.lower() not in PUBLISHABLE:
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            raise CloudflareError(
                f"{path.name} is larger than Cloudflare accepts (25MB)."
            )
        out.append(path)

    if not out:
        raise CloudflareError(
            f"Nothing publishable in {folder}. A site needs at least an "
            "index.html."
        )
    # Somewhere in the tree, not necessarily at the top: the whole sites
    # repo is published in one go, with each business in its own folder, so
    # the root is a directory of sites rather than a site itself.
    if not any(p.name.lower() == "index.html" for p in out):
        raise CloudflareError(
            f"{folder} contains no index.html anywhere, so Cloudflare would "
            "serve nothing. Check the folder is the sites repo and that AI "
            "Studio has pushed at least one site into it."
        )
    return out


class CloudflareClient:
    """Cloudflare Pages, over the v4 API."""

    def __init__(
        self,
        token: str,
        account_id: str,
        *,
        session=None,
        timeout: float = 60.0,
    ) -> None:
        if not token:
            raise CloudflareError(
                "CLOUDFLARE_API_TOKEN is not set. Create one at "
                "dash.cloudflare.com > My Profile > API Tokens with the "
                "'Cloudflare Pages: Edit' permission, then put it in .env."
            )
        if not account_id:
            raise CloudflareError(
                "CLOUDFLARE_ACCOUNT_ID is not set. It is in the URL when you "
                "are signed in to dash.cloudflare.com, and on the right of "
                "the account's overview page."
            )
        self._token = token
        self._account = account_id
        self._session = session or requests.Session()
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def list_projects(self) -> list[str]:
        """Pages project names on this account. The cheapest proof the token
        and account id are both right."""
        data = self._get(f"/accounts/{self._account}/pages/projects")
        return [
            str(p.get("name", ""))
            for p in (data.get("result") or [])
            if p.get("name")
        ]

    def publish(self, project: str, folder: Path) -> Deployment:
        """Upload a folder as a new deployment of ``project``."""
        files = site_files(Path(folder))
        parts = []
        opened = []
        try:
            for path in files:
                # The name Cloudflare serves it under, always with forward
                # slashes -- a Windows backslash here becomes part of the
                # filename rather than a directory.
                rel = path.relative_to(folder).as_posix()
                handle = path.open("rb")
                opened.append(handle)
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                parts.append(("file", (rel, handle, mime)))

            resp = self._session.post(
                f"{API}/accounts/{self._account}/pages/projects/{project}/deployments",
                headers=self._headers,
                files=parts,
                timeout=self._timeout,
            )
        finally:
            for handle in opened:
                handle.close()

        data = self._json(resp, what=f"publishing to {project}")
        result = data.get("result") or {}
        url = str(result.get("url") or "")
        if not url:
            raise CloudflareError(
                f"Cloudflare accepted the upload but returned no URL for "
                f"{project}. Raw reply: {str(data)[:300]}"
            )
        return Deployment(
            url=url, deployment_id=str(result.get("id") or ""), project=project
        )

    # ---------------------------------------------------------------- http

    def _get(self, path: str) -> dict:
        resp = self._session.get(
            f"{API}{path}", headers=self._headers, timeout=self._timeout
        )
        return self._json(resp, what=path)

    def _json(self, resp, *, what: str) -> dict:
        try:
            data = resp.json()
        except ValueError:
            raise CloudflareError(
                f"Cloudflare answered {resp.status_code} with something that "
                f"is not JSON, while {what}."
            )

        if resp.status_code == 401 or resp.status_code == 403:
            raise CloudflareError(
                "Cloudflare rejected the token. Check CLOUDFLARE_API_TOKEN, "
                "and that it carries the 'Cloudflare Pages: Edit' permission "
                "for this account."
            )
        if not data.get("success", resp.ok):
            errors = data.get("errors") or []
            detail = "; ".join(
                str(e.get("message", e)) for e in errors
            ) or f"HTTP {resp.status_code}"
            raise CloudflareError(f"Cloudflare refused while {what}: {detail}")
        return data
