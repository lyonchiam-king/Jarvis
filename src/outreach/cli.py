"""Command line entry point.

Every stage is independently runnable, so you can go step by step or chain
them:

    outreach preflight
    outreach init-sheet
    outreach run discovery --dry-run
    outreach run discovery
"""

from __future__ import annotations

import json
import logging
import sys

import click

from . import compliance, schema
from .auth import AuthError, get_credentials
from .config import Config, ConfigError, load_config
from .providers.gmail import GmailClient
from .providers.llm import BriefWriter, LLMError
from .providers.lovable import LovableAdapter, LovableUnavailable
from .providers.places import PlacesClient, PlacesError
from .sheets import SheetsClient, SheetsError
from .stage import Stage
from .stages import discovery as discovery_stage
from .stages import init_sheet as init_sheet_stage
from .stages import outreach_draft as outreach_stage
from .stages import research as research_stage
from .stages import site_brief as site_brief_stage

log = logging.getLogger("outreach")

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


def _credentials(config: Config):
    try:
        return get_credentials(
            config.secrets.oauth_client_file, config.secrets.token_file
        )
    except AuthError as exc:
        raise click.ClickException(str(exc)) from exc


def _writer(config: Config) -> BriefWriter:
    try:
        return BriefWriter(
            config.secrets.anthropic_api_key,
            model=config.research.model,
            effort=config.research.effort,
        )
    except LLMError as exc:
        raise click.ClickException(str(exc)) from exc


def _sheets_client(config: Config, *, require_sheet_id: bool = True) -> SheetsClient:
    creds = _credentials(config)

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
            creds = _credentials(config)
            click.echo("[ OK ] Google OAuth token valid")

            # Check the APIs are actually switched on. Without this, preflight
            # passes and then `init-sheet` fails on a Cloud setting -- which is
            # exactly what happened before this check existed.
            sheets = SheetsClient(creds, config.secrets.sheet_id)
            sheets_ok, sheets_detail = sheets.probe()
            if sheets_ok:
                click.echo("[ OK ] Google Sheets API enabled")
            else:
                click.echo(f"[FAIL] {sheets_detail}")
                ok = False

            gmail_ok, gmail_detail = GmailClient(creds).probe()
            if gmail_ok:
                click.echo(f"[ OK ] Gmail API {gmail_detail}")
            else:
                # Stage 5 only. Don't fail the whole preflight over it.
                click.echo(f"[WARN] Gmail not ready (needed for Stage 5): {gmail_detail}")

            if config.secrets.sheet_id and sheets_ok:
                titles = sheets.existing_tab_titles()
                missing = [t.title for t in schema.ALL_TABS if t.title not in titles]
                if missing:
                    click.echo(f"[WARN] Sheet is missing tabs: {', '.join(missing)}")
                    click.echo("       Run `outreach init-sheet` to create them.")
                else:
                    click.echo("[ OK ] Sheet reachable, all tabs present")
                click.echo(f"       {sheets.url}")
            elif not config.secrets.sheet_id:
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


@main.command("dashboard")
@click.option("--port", default=8765, help="Port to serve on.")
@click.option("--no-browser", is_flag=True, help="Don't open a browser tab.")
@click.pass_context
def dashboard_cmd(ctx: click.Context, port: int, no_browser: bool) -> None:
    """Open the dashboard in your browser. Runs on this machine only."""
    from .dashboard import serve

    serve(ctx.obj["config_path"], port=port, open_browser=not no_browser)


@main.command("compliance")
@click.pass_context
def compliance_cmd(ctx: click.Context) -> None:
    """Show the outreach rules on file for this campaign's country."""
    config = _load(ctx.obj["config_path"])
    click.echo(compliance.format_notice(config.compliance.country_code))
    click.echo("")
    click.echo(f"Acknowledged in config: {config.compliance.acknowledged}")


@main.group()
def briefs() -> None:
    """Read and record site briefs -- the hooks an MCP client drives."""


@briefs.command("pending")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def briefs_pending(ctx: click.Context, as_json: bool) -> None:
    """List approved briefs that have no site yet.

    Exists so an MCP client (Claude Code with the Lovable connector) can read
    the queue, build each one, and report back -- the step this pipeline
    cannot take itself, because Lovable offers no headless credential.
    """
    config = _load(ctx.obj["config_path"])
    sheets = _sheets_client(config)
    rows = sheets.read_rows(schema.SITE_BRIEFS)

    queue = [
        {
            "lead_id": lead_id,
            "business_name": row.get("Business Name", ""),
            "prompt": row.get("Lovable Prompt", ""),
        }
        for lead_id, (_n, row) in rows.items()
        if (row.get("Approval Status") or "").strip().casefold() == "approved"
        and not (row.get("Lovable Project ID") or "").strip()
    ]

    if as_json:
        click.echo(json.dumps(queue, indent=2))
        return

    if not queue:
        click.echo("Nothing approved and unbuilt.")
        return
    click.echo(f"{len(queue)} approved brief(s) waiting to be built:\n")
    for item in queue:
        click.echo(f"  {item['lead_id']}  {item['business_name']}")


@briefs.command("record")
@click.option("--lead-id", required=True, help="The lead's place_id.")
@click.option("--preview-url", default="", help="URL of the finished site.")
@click.option("--project-id", default="", help="Lovable project ID, if you have it.")
@click.pass_context
def briefs_record(
    ctx: click.Context, lead_id: str, preview_url: str, project_id: str
) -> None:
    """Record a built site and advance the lead to Site Built.

    The preview URL is not bookkeeping: Stage 5 puts it in the outreach email.
    """
    if preview_url and not preview_url.startswith(("http://", "https://")):
        raise click.ClickException("--preview-url must start with http:// or https://")

    config = _load(ctx.obj["config_path"])
    sheets = _sheets_client(config)

    sheets.update_column(
        schema.SITE_BRIEFS,
        "Lovable Project ID",
        {lead_id: project_id or "built-via-mcp"},
    )
    if preview_url:
        sheets.update_column(schema.SITE_BRIEFS, "Preview URL", {lead_id: preview_url})
    sheets.update_column(schema.LEADS, "Stage", {lead_id: Stage.SITE_BUILT.value})

    click.echo(f"Recorded {lead_id} as Site Built.")
    if preview_url:
        click.echo(f"  {preview_url}")


@main.group()
def run() -> None:
    """Run a pipeline stage."""


@run.command("discovery")
@click.option("--dry-run", is_flag=True, help="Search and report, but write nothing.")
@click.option(
    "--refresh",
    is_flag=True,
    help="Update businesses already on the Sheet instead of hunting for new ones.",
)
@click.option("--limit", type=int, default=None, help="Find fewer than the batch size.")
@click.pass_context
def run_discovery(ctx: click.Context, dry_run: bool, refresh: bool, limit: int | None) -> None:
    """Stage 1 -- find businesses with no website and write them to Leads.

    Every run looks for businesses you have never seen: anything already on the
    Leads tab is skipped and doesn't use up the batch. Run it daily and you get
    a fresh batch each time, until the campaign's areas are used up.
    """
    config = _load(ctx.obj["config_path"])

    try:
        places = PlacesClient(config.secrets.places_api_key)
    except PlacesError as exc:
        raise click.ClickException(str(exc)) from exc

    # A dry run touches Places only, so it needs no Google credentials at all.
    sheets = None if dry_run else _sheets_client(config)

    report = discovery_stage.run(
        config, places, sheets, dry_run=dry_run, refresh=refresh, limit=limit
    )

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
    if not dry_run:
        _echo_sheet_link(config)


@run.command("research")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Process fewer than the configured batch size (for a trial run).",
)
@click.option(
    "--social-first/--sheet-order",
    "social_first",
    default=None,
    help=(
        "Research businesses that have a Facebook or Instagram page before "
        "those with no web presence. Defaults to research.social_first."
    ),
)
@click.pass_context
def run_research(ctx: click.Context, limit: int | None, social_first: bool | None) -> None:
    """Stage 2 -- research each discovered lead into a structured brief."""
    config = _load(ctx.obj["config_path"])

    try:
        places = PlacesClient(config.secrets.places_api_key)
    except PlacesError as exc:
        raise click.ClickException(str(exc)) from exc

    writer = _writer(config)
    sheets = _sheets_client(config)
    report = research_stage.run(
        config, places, sheets, writer, limit=limit, social_first=social_first
    )

    click.echo("")
    for line in report.summary_lines():
        click.echo(line)
    _echo_sheet_link(config)


@run.command("site-brief")
@click.option("--limit", type=int, default=None, help="Process fewer than the batch size.")
@click.option(
    "--social-first/--sheet-order",
    "social_first",
    default=None,
    help=(
        "Work through businesses that have a Facebook or Instagram page "
        "before those with no web presence. Defaults to research.social_first."
    ),
)
@click.pass_context
def run_site_brief(ctx: click.Context, limit: int | None, social_first: bool | None) -> None:
    """Stage 4 -- write a ready-to-paste Lovable prompt per researched lead."""
    config = _load(ctx.obj["config_path"])
    sheets = _sheets_client(config)
    lovable = LovableAdapter(
        enabled=config.lovable.enabled,
        require_confirmation=config.lovable.require_confirmation,
        confirm=click.confirm,
    )
    report = site_brief_stage.run(
        config, sheets, _writer(config), lovable,
        limit=limit, social_first=social_first,
    )

    click.echo("")
    for line in report.summary_lines():
        click.echo(line)
    click.echo("")
    click.echo(
        "Prompts are in the Site Briefs tab. Set Approval Status to 'Approved' "
        "on the ones you want built, then run `outreach run site-build` "
        "(or just paste the prompt into Lovable yourself)."
    )
    _echo_sheet_link(config)


@run.command("site-build")
@click.option("--limit", type=int, default=None, help="Process fewer than the batch size.")
@click.pass_context
def run_site_build(ctx: click.Context, limit: int | None) -> None:
    """Stage 4b -- build the approved sites via Lovable. Spends credits."""
    config = _load(ctx.obj["config_path"])
    sheets = _sheets_client(config)
    lovable = LovableAdapter(
        enabled=config.lovable.enabled,
        require_confirmation=config.lovable.require_confirmation,
        confirm=click.confirm,
    )
    try:
        report = site_brief_stage.build_approved(config, sheets, lovable, limit=limit)
    except LovableUnavailable as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo("")
    for line in report.summary_lines():
        click.echo(line)
    _echo_sheet_link(config)


@run.command("outreach-draft")
@click.option("--limit", type=int, default=None, help="Process fewer than the batch size.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Write the copy and print it, but create no Gmail drafts and touch no Sheet.",
)
@click.option(
    "--social-first/--sheet-order",
    "social_first",
    default=None,
    help=(
        "Work through businesses that have a Facebook or Instagram page "
        "before those with no web presence. Defaults to research.social_first."
    ),
)
@click.pass_context
def run_outreach_draft(
    ctx: click.Context, limit: int | None, dry_run: bool, social_first: bool | None
) -> None:
    """Stage 5 -- draft the email into Gmail and the WhatsApp text into the Sheet."""
    config = _load(ctx.obj["config_path"])

    try:
        outreach_stage.preflight(config)
    except (compliance.ComplianceBlocked, outreach_stage.MissingPostalAddress) as exc:
        raise click.ClickException(str(exc)) from exc

    sheets = _sheets_client(config)
    gmail = None if dry_run else GmailClient(_credentials(config))

    report = outreach_stage.run(
        config, sheets, _writer(config), gmail,
        limit=limit, dry_run=dry_run, social_first=social_first,
    )

    click.echo("")
    for line in report.summary_lines():
        click.echo(line)
    if not dry_run:
        click.echo("")
        click.echo(
            "Drafts are in your Gmail drafts folder. Nothing was sent -- "
            "review each one and press send yourself."
        )
    _echo_sheet_link(config)


def _echo_sheet_link(config: Config) -> None:
    if config.secrets.sheet_id:
        click.echo("")
        click.echo(
            f"https://docs.google.com/spreadsheets/d/{config.secrets.sheet_id}/edit"
        )


if __name__ == "__main__":
    main()
