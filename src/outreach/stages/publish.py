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

log = logging.getLogger("outreach")


@dataclass
class PublishReport:
    approved: int = 0
    already_live: int = 0
    no_folder: int = 0
    published: int = 0
    failed: int = 0
    urls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"Approved and built      {self.approved}",
            f"  already published     {self.already_live}  (skipped)",
            f"  published this run    {self.published}",
        ]
        if self.no_folder:
            out.append(
                f"  no folder in the repo {self.no_folder}  (skipped -- push "
                "them from AI Studio first, then pull)"
            )
        if self.failed:
            out.append(f"  failed                {self.failed}")
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


def run(config, sheets, client=None, *, limit: int = 0) -> PublishReport:
    """Publish every approved, built, unpublished site."""
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

    published: dict[str, str] = {}
    for lead_id, (_row, brief) in briefs.items():
        if (brief.get("Approval Status") or "").strip().casefold() != "approved":
            continue
        report.approved += 1

        # An address already recorded means this is done. Publishing again
        # would spend an upload to produce the same page at a new deployment
        # URL, and the message may already have gone out with the old one.
        if (brief.get("Preview URL") or "").strip():
            report.already_live += 1
            continue

        entry = leads.get(lead_id)
        name = (entry[1].get("Business Name") if entry else "") or lead_id

        folder = folder_for(repo, name, lead_id)
        if folder is None:
            report.no_folder += 1
            continue

        if limit and report.published >= limit:
            break

        try:
            deployment = client.publish(project, folder)
        except CloudflareError as exc:
            report.failed += 1
            report.errors.append(f"{name}: {exc}")
            log.warning("Could not publish %s: %s", name, exc)
            continue

        published[lead_id] = deployment.url
        report.published += 1
        report.urls.append(f"{name}: {deployment.url}")

    if published:
        sheets.update_column(schema.SITE_BRIEFS, "Preview URL", published)

    return report
