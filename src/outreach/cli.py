"""Command line entry point.

Every stage is independently runnable, so you can go step by step or chain
them:

    outreach preflight
    outreach init-sheet
    outreach run discovery --dry-run
    outreach run discovery
"""

from __future__ import annotations

import logging
import sys

import click

from . import compliance, schema
from .auth import AuthError, get_credentials
from .config import Config, ConfigError, load_config
from .providers.places import PlacesClient, PlacesError
from .sheets import SheetsClient, SheetsError
from .stages import discovery as discovery_stage
from .stages import init_sheet as init_sheet_stage

log = logging.getLogger("outreach")

NOT_BUILT = {
    "research": "Stage 2 (research)",
    "email-discovery": "Stage 2b (email discovery)",
    "site-brief": "Stage 4 (Lovable site briefs)",
    "outreach-draft": "Stage 5 (Gmail drafts)",
}


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )


def _load(config_path: str | None) -> Config:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _sheets_client(config: Config, *, require_sheet_id: bool = True) -> SheetsClient:
    try:
        creds = get_credentials(
            config.secrets.oauth_client_file, config.secrets.token_file
        )
    except AuthError as exc:
        raise click.ClickException(str(exc)) from exc

    if require_sheet_id and not config.secrets.sheet_id:
        raise click.ClickException(
            "SHEET_ID is not set in .env.\n"
            "Run `outreach init-sheet` first -- it creates the spreadsheet and "
            "prints the ID to paste in."
        )
    return SheetsClient(creds, config.secrets.sheet_id)


@click.group()
@click.option("--config", "config_path", default=None, help="Path to a campaign YAML.")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, verbose: bool) -> None:
    """Lead discovery and outreach pipeline for SMEs without websites."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@main.command()
@click.pass_context
def preflight(ctx: click.Context) -> None:
    """Check config, API key and Google access before spending anything."""
    config = _load(ctx.obj["config_path"])
    ok = True

    click.echo(f"Campaign config   {config.source_path}")
    click.echo(f"  campaign        {config.campaign.name}")
    click.echo(f"  query           {config.campaign.search_query!r}")
    click.echo(f"  country         {config.compliance.country_code}")
    click.echo("")

    # --- Places -----------------------------------------------------------
    if not config.secrets.places_api_key:
        click.echo("[FAIL] PLACES_API_KEY is not set in .env")
        ok = False
    else:
        click.echo("Testing Places API (one request, ~1 result)...")
        try:
            client = PlacesClient(config.secrets.places_api_key)
            results = list(
                client.search_text(
                    config.campaign.search_query,
                    page_size=1,
                    language_code=config.campaign.language,
                    max_pages=1,
                )
            )
            click.echo(f"[ OK ] Places API reachable ({len(results)} result returned)")
            if results:
                click.echo(f"       sample: {results[0].name}")
        except PlacesError as exc:
            click.echo(f"[FAIL] {exc}")
            ok = False

    click.echo("")

    # --- Google OAuth + Sheets -------------------------------------------
    if not config.secrets.oauth_client_file.exists():
        click.echo(f"[FAIL] OAuth client file missing: {config.secrets.oauth_client_file}")
        click.echo("       See README 'First-time Google Cloud setup'.")
        ok = False
    else:
        try:
            sheets = _sheets_client(config, require_sheet_id=False)
            click.echo("[ OK ] Google OAuth token valid")
            if config.secrets.sheet_id:
                titles = sheets.existing_tab_titles()
                missing = [t.title for t in schema.ALL_TABS if t.title not in titles]
                if missing:
                    click.echo(f"[WARN] Sheet is missing tabs: {', '.join(missing)}")
                    click.echo("       Run `outreach init-sheet` to create them.")
                else:
                    click.echo("[ OK ] Sheet reachable, all tabs present")
                click.echo(f"       {sheets.url}")
            else:
                click.echo("[WARN] SHEET_ID not set -- run `outreach init-sheet`")
        except (click.ClickException, SheetsError) as exc:
            click.echo(f"[FAIL] {exc}")
            ok = False

    click.echo("")
    click.echo(compliance.format_notice(config.compliance.country_code))
    click.echo("")
    click.echo("Preflight passed." if ok else "Preflight FAILED -- fix the items above.")
    if not ok:
        raise SystemExit(1)


@main.command("init-sheet")
@click.option("--title", default=None, help="Title for a newly created spreadsheet.")
@click.pass_context
def init_sheet(ctx: click.Context, title: str | None) -> None:
    """Create the campaign spreadsheet, or add missing tabs to an existing one."""
    config = _load(ctx.obj["config_path"])
    sheets = _sheets_client(config, require_sheet_id=False)
    try:
        sheet_id, new_tabs = init_sheet_stage.run(config, sheets, title=title)
    except SheetsError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Spreadsheet ready: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    click.echo(f"Tabs created: {', '.join(new_tabs) if new_tabs else '(none -- all existed)'}")
    if not config.secrets.sheet_id:
        click.echo("")
        click.echo("Add this line to your .env:")
        click.echo(f"  SHEET_ID={sheet_id}")


@main.command("compliance")
@click.pass_context
def compliance_cmd(ctx: click.Context) -> None:
    """Show the outreach rules on file for this campaign's country."""
    config = _load(ctx.obj["config_path"])
    click.echo(compliance.format_notice(config.compliance.country_code))
    click.echo("")
    click.echo(f"Acknowledged in config: {config.compliance.acknowledged}")


@main.group()
def run() -> None:
    """Run a pipeline stage."""


@run.command("discovery")
@click.option("--dry-run", is_flag=True, help="Search and report, but write nothing.")
@click.pass_context
def run_discovery(ctx: click.Context, dry_run: bool) -> None:
    """Stage 1 -- find businesses with no website and write them to Leads."""
    config = _load(ctx.obj["config_path"])

    try:
        places = PlacesClient(config.secrets.places_api_key)
    except PlacesError as exc:
        raise click.ClickException(str(exc)) from exc

    # A dry run touches Places only, so it needs no Google credentials at all.
    sheets = None if dry_run else _sheets_client(config)

    report = discovery_stage.run(config, places, sheets, dry_run=dry_run)

    click.echo("")
    for line in report.summary_lines():
        click.echo(line)
    if report.upsert.inserted_names:
        click.echo("")
        click.echo("New leads:")
        for name in report.upsert.inserted_names[:25]:
            click.echo(f"  - {name}")
        remaining = len(report.upsert.inserted_names) - 25
        if remaining > 0:
            click.echo(f"  ... and {remaining} more")
    if not dry_run and config.secrets.sheet_id:
        click.echo("")
        click.echo(
            f"https://docs.google.com/spreadsheets/d/{config.secrets.sheet_id}/edit"
        )


for _name, _label in NOT_BUILT.items():

    @run.command(_name)
    def _not_built(_label: str = _label) -> None:
        """Not built yet."""
        raise click.ClickException(
            f"{_label} is not built yet. Stage 1 (discovery) is complete and "
            "verified first, by design."
        )


if __name__ == "__main__":
    main()
