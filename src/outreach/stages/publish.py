"""Put a built site on the internet, and record where it landed.

The sites are built in Google AI Studio and pushed from there into a GitHub
repository. This stage reads them out of a local clone of that repo and
uploads them to Cloudflare Pages, then writes the resulting address back to
the Sheet so the outreach message has a link to point at.

It never writes to the repository. What is in git stays git's -- this only
reads, so a publish can never conflict with a `git pull`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .. import schema
from ..providers.cloudflare import CloudflareClient, CloudflareError
from ..slug import site_slug
from ..stage import Stage

log = logging.getLogger("outreach")


@dataclass
class PublishReport:
    approved: int = 0
    already_live: int = 0
    disqualified: int = 0
    no_folder: int = 0
    published: int = 0
    failed: int = 0
    deployment: str = ""
    urls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"Approved and built      {self.approved}",
            f"  already published     {self.already_live}  (skipped)",
        ]
        if self.disqualified:
            out.append(
                f"  lead disqualified     {self.disqualified}  (skipped)"
            )
        out.append(f"  published this run    {self.published}")
        if self.no_folder:
            out.append(
                f"  no folder in the repo {self.no_folder}  (skipped -- push "
                "them from AI Studio first, then pull)"
            )
        if self.failed:
            out.append(f"  failed                {self.failed}")
        if self.deployment:
            out.append(f"Deployment              {self.deployment}")
        for url in self.urls:
            out.append(f"    {url}")
        for err in self.errors[:5]:
            out.append(f"    {err}")
        return out


def folder_for(repo: Path, name: str, lead_id: str) -> Path | None:
    """This lead's site folder inside the repo, if it is there yet.

    The slug is the same one the rest of the pipeline uses, so a folder
    exported from AI Studio under that name is found without anything
    needing to be recorded. A plain business-name folder is accepted too,
    because that is what a person naming it by hand would type.
    """
    slug = site_slug(name, lead_id)
    candidates = [repo / slug, repo / name, repo / name.replace(" ", "-").lower()]
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate
    return None


def public_url(config, folder_name: str) -> str:
    """Where a business's page will answer once the repo is published.

    A path under one project, not a deployment of its own. A Pages
    deployment is a full snapshot of the site rather than an addition to it,
    so uploading a single business would produce a live site containing only
    that business and every page published before it would vanish from the
    main address.
    """
    base = (config.build.public_base_url or "").strip().rstrip("/")
    if not base:
        base = f"https://{config.build.cloudflare_project}.pages.dev"
    return f"{base}/{folder_name}/"


def run(config, sheets, client=None, *, limit: int = 0) -> PublishReport:
    """Publish the whole sites repo, and record each lead's address."""
    report = PublishReport()

    repo_dir = (config.build.sites_repo_dir or "").strip()
    if not repo_dir:
        raise CloudflareError(
            "build.sites_repo_dir is not set. Point it at your local clone "
            "of the repository AI Studio pushes the sites into."
        )
    repo = Path(repo_dir)
    if not repo.is_dir():
        raise CloudflareError(
            f"build.sites_repo_dir points at {repo}, which is not a folder. "
            "Clone the sites repository there, or correct the path."
        )

    project = (config.build.cloudflare_project or "").strip()
    if not project:
        raise CloudflareError(
            "build.cloudflare_project is not set. Run `outreach "
            "cloudflare-check` to see the project names on your account."
        )

    client = client or CloudflareClient(
        config.secrets.cloudflare_api_token, config.secrets.cloudflare_account_id
    )

    leads = sheets.read_rows(schema.LEADS)
    briefs = sheets.read_rows(schema.SITE_BRIEFS)

    # Who is waiting for an address, and which folder is theirs.
    waiting: dict[str, str] = {}
    for lead_id, (_row, brief) in briefs.items():
        if (brief.get("Approval Status") or "").strip().casefold() != "approved":
            continue
        report.approved += 1

        # An address already recorded means this is done. The page is part
        # of every later deployment anyway, so there is nothing to redo.
        if (brief.get("Preview URL") or "").strip():
            report.already_live += 1
            continue

        # A lead disqualified after its brief was approved must not get a
        # public address. Publishing is the step where the outside world
        # starts seeing the page, so this is the last gate that can stop it.
        entry = leads.get(lead_id)
        if entry is not None:
            if (entry[1].get("Stage") or "").strip() == Stage.DISQUALIFIED.value:
                report.disqualified += 1
                continue
        name = (entry[1].get("Business Name") if entry else "") or lead_id
        folder = folder_for(repo, name, lead_id)
        if folder is None:
            report.no_folder += 1
            continue
        waiting[lead_id] = folder.name

    if not waiting:
        log.info("Nothing to publish -- every approved site already has a URL.")
        return report

    # One upload of the whole repo, however many leads are waiting. Each
    # business keeps its own path, and pages published earlier stay up.
    try:
        deployment = client.publish(project, repo)
    except CloudflareError as exc:
        report.failed = len(waiting)
        report.errors.append(str(exc))
        log.warning("Publish failed: %s", exc)
        return report

    published = {
        lead_id: public_url(config, folder_name)
        for lead_id, folder_name in waiting.items()
    }
    if limit:
        published = dict(list(published.items())[:limit])

    sheets.update_column(schema.SITE_BRIEFS, "Preview URL", published)
    report.published = len(published)
    report.deployment = deployment.url
    report.urls = list(published.values())
    return report
