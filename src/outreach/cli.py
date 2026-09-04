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
from .providers.llm import LLMError, make_writer
from .providers.lovable import LovableAdapter, LovableSkipped, LovableUnavailable
from .providers.places import PlacesClient, PlacesError
from .sheets import SheetsClient, SheetsError
from .stage import Stage, advance
from .stages import discovery as discovery_stage
from .stages import init_sheet as init_sheet_stage
from .stages import outreach_draft as outreach_stage
from .stages import research as research_stage
from .stages import reachability as reachability_stage
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


def _writer(config: Config):
    """The model this campaign uses -- Anthropic, Ollama, OpenRouter, whatever."""
    try:
        return make_writer(config)
    except LLMError as exc:
        raise click.ClickException(str(exc)) from exc


def _outreach_writer(config: Config):
    """The model that writes outreach copy -- a smaller one, if configured."""
    try:
        return make_writer(config, model=config.outreach.model or None)
    except LLMError as exc:
        raise click.ClickException(str(exc)) from exc


def _image_client(config: Config, *, require_model: bool = True):
    """The Vertex client for `run site-generate`'s images, or None.

    Checking `image_provider` before anything else in this function matters
    more than it looks: `auth.vertex_credentials` is an OAuth call -- a
    browser consent flow the first time, a token refresh after -- and the
    default campaign never sets `image_provider` to "vertex". Someone who has
    never touched this feature must be able to run `site-generate` and have
    this function construct nothing and call nothing, not silently pay for an
    OAuth round trip on a token they never asked to create.
    """
    if config.build.image_provider != "vertex":
        return None

    from . import auth
    from .providers.vertex_image import VertexImageClient

    # Checked here rather than at config load: `vertex-models` is how you find
    # a model name, and it needs a loadable config to know the project. It
    # passes require_model=False so it can run before one is chosen.
    if require_model and not config.build.vertex_image_model:
        raise click.ClickException(
            "build.vertex_image_model is not set. Run `outreach vertex-models` "
            "to see what your project offers, then `outreach vertex-check "
            "--model <name>` to prove one works before pinning it in the "
            "campaign file."
        )

    try:
        creds = auth.vertex_credentials(
            config.secrets.oauth_client_file, config.secrets.vertex_token_file
        )
    except AuthError as exc:
        raise click.ClickException(str(exc)) from exc

    return VertexImageClient(
        # A closure, not a captured token string: an OAuth access token lives
        # one hour and a sixty-page overnight run outlives that. Calling
        # `access_token(creds)` fresh for every request is what lets it
        # refresh the cached token in place instead of going stale partway
        # through the run.
        lambda: auth.access_token(creds),
        project=config.secrets.vertex_project_id,
        region=config.build.vertex_region,
        model=config.build.vertex_image_model,
        image_size=config.build.vertex_image_size,
    )


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
    click.echo(
        f"  model           {config.research.model} "
        f"via {config.research.provider}"
        + (f" ({config.research.base_url})" if config.research.base_url else "")
    )
    click.echo("")

    # --- the model ---------------------------------------------------------
    from .config import PROVIDER_KEY_ENV

    if config.research.provider == "ollama":
        click.echo("[ OK ] Local Ollama needs no API key")
    elif not config.secrets.llm_api_key:
        env_name = PROVIDER_KEY_ENV.get(config.research.provider, "the API key")
        click.echo(f"[FAIL] {env_name} is not set in .env")
        ok = False
    else:
        click.echo(f"[ OK ] {PROVIDER_KEY_ENV[config.research.provider]} is set")
    if config.research.provider != "anthropic":
        click.echo(
            "[WARN] Only Anthropic guarantees the reply matches the schema. "
            "Other providers are validated and retried once, then the lead is "
            "skipped -- watch the Errors tab on your first run."
        )
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


@main.command("vertex-probe")
@click.option("--limit", type=int, default=0, help="Stop after this many working combinations.")
@click.pass_context
def vertex_probe(ctx: click.Context, limit: int) -> None:
    """Find a model, region and API version that actually work, by trying them.

    Vertex refuses a wrong model, a wrong region and a wrong API version with
    the same 404, so a person tries combinations one at a time and cannot
    tell which axis is wrong. This sweeps them and prints what worked. A 404
    costs nothing, so the sweep is free apart from the seconds it takes; only
    a combination that *succeeds* generates an image and spends anything.
    """
    from dataclasses import replace as _replace

    from .providers.sitegen import SiteGenError

    config = _load(ctx.obj["config_path"])
    click.echo(f"Campaign config   {config.source_path}")
    if config.build.image_provider != "vertex":
        raise click.ClickException(
            "build.image_provider is not 'vertex'. Set it under `build:` first."
        )

    base = _image_client(config, require_model=False)
    try:
        catalogue = [n for n in base.list_models() if "image" in n.lower()]
    except SiteGenError as exc:
        raise click.ClickException(str(exc)) from exc
    if not catalogue:
        raise click.ClickException(
            "Google publishes no image models to this account. Check the "
            "Vertex AI API is enabled on the project."
        )

    regions = [config.build.vertex_region, "global", "us-central1", "asia-southeast1"]
    seen: list[str] = []
    for r in regions:
        if r and r not in seen:
            seen.append(r)

    click.echo("")
    click.echo(
        f"Trying {len(catalogue)} model(s) x {len(seen)} region(s) x 2 API versions. "
        "Only a success costs anything."
    )
    click.echo("")

    working: list[tuple[str, str, str]] = []
    for model in catalogue:
        for region in seen:
            for version in ("v1beta1", "v1"):
                probe_config = _replace(
                    config,
                    build=_replace(
                        config.build, vertex_image_model=model, vertex_region=region
                    ),
                )
                client = _image_client(probe_config)
                client.api_version = version
                try:
                    data, _mime = client.generate_image(
                        "A soft out-of-focus warm gradient. Texture only, no objects, "
                        "no people, no text."
                    )
                except SiteGenError as exc:
                    code = exc.status_code or "?"
                    click.echo(f"  [{code:>3}] {model}  {region}  {version}")
                    continue
                click.echo(
                    f"  [ OK ] {model}  {region}  {version}  ({len(data):,} bytes)"
                )
                working.append((model, region, version))
                if limit and len(working) >= limit:
                    break
            if limit and len(working) >= limit:
                break
        if limit and len(working) >= limit:
            break

    click.echo("")
    if not working:
        raise click.ClickException(
            "No combination worked. Every one returned an error above -- if they "
            "are all 403, the Vertex AI API is probably not enabled on this "
            "project, or the project is not linked to the billing account "
            "holding your credits."
        )

    model, region, version = working[0]
    click.echo("Put this in your campaign file under `build:`")
    click.echo(f"  vertex_image_model: \"{model}\"")
    click.echo(f"  vertex_region: {region}")
    if version != "v1beta1":
        click.echo(f"  vertex_api_version: {version}")
    click.echo("")
    click.echo(f"Then: outreach vertex-check")


@main.command("vertex-models")
@click.pass_context
def vertex_models(ctx: click.Context) -> None:
    """List the Vertex image models Google publishes.

    This is Google's catalogue, not proof your project may call any given
    one -- the only proof of that is generating an image, which is what
    `outreach vertex-check` does.
    """
    config = _load(ctx.obj["config_path"])
    client = _image_client(config, require_model=False)
    if client is None:
        raise click.ClickException(
            "build.image_provider is not 'vertex', so there is nothing to list. "
            "Set it under `build:` in your campaign file first."
        )

    from .providers.sitegen import SiteGenError

    try:
        names = client.list_models()
    except SiteGenError as exc:
        raise click.ClickException(str(exc)) from exc

    images = [n for n in names if "image" in n.lower()]
    click.echo("")
    click.echo("Image models Google publishes:")
    for name in images or names:
        click.echo(f"  {name}")
    click.echo("")
    click.echo(
        "This is Google's catalogue, not proof your project can call one. "
        "Put a name in build.vertex_image_model, then run "
        "`outreach vertex-check` to prove it."
    )


@main.command("vertex-check")
@click.option("--model", default=None, help="Try this model instead of the configured one.")
@click.option(
    "--region",
    default=None,
    help="Try this region instead of the configured one, e.g. us-central1.",
)
@click.pass_context
def vertex_check(ctx: click.Context, model: str | None, region: str | None) -> None:
    """Prove the whole Vertex chain, ending in one real image on your disk.

    Ordered so the first failure is the real one: a missing project ID
    reported after a confusing auth error would send you to fix the wrong
    thing.
    """
    from pathlib import Path

    from .providers.sitegen import SiteGenError

    config = _load(ctx.obj["config_path"])
    if region:
        from dataclasses import replace as _replace

        config = _replace(config, build=_replace(config.build, vertex_region=region.strip().lower()))

    # Printed first, and deliberately: if the dashboard has ever saved a
    # setting, the live file is the *.local.yaml twin, and edits to the
    # tracked file are a silent no-op that looks exactly like broken code.
    click.echo(f"Campaign config   {config.source_path}")

    if config.build.image_provider != "vertex":
        click.echo("[FAIL] build.image_provider is not 'vertex'")
        raise click.ClickException(
            "Set `image_provider: vertex` under `build:` in the campaign file "
            "named above, then run this again."
        )

    if not config.secrets.vertex_project_id:
        click.echo("[FAIL] VERTEX_PROJECT_ID is not set")
        raise click.ClickException(
            "Add a line to .env:\n    VERTEX_PROJECT_ID=your-project-id\n"
            "It is the Project ID from the Google Cloud Console project picker "
            "-- the short text one, not the number and not the display name."
        )
    click.echo(f"[ OK ] VERTEX_PROJECT_ID is set ({config.secrets.vertex_project_id})")

    try:
        client = _image_client(config, require_model=False)
    except click.ClickException:
        click.echo("[FAIL] Could not sign in for Google Cloud")
        raise
    click.echo(f"[ OK ] Signed in for Google Cloud ({config.secrets.vertex_token_file})")
    click.echo("       Your Sheets and Gmail login is a different file and is untouched.")

    try:
        names = client.list_models()
    except SiteGenError as exc:
        click.echo("[FAIL] Vertex did not answer")
        raise click.ClickException(str(exc)) from exc
    click.echo(f"[ OK ] Vertex reachable -- {len(names)} models listed")

    chosen = model or config.build.vertex_image_model
    if not chosen:
        click.echo("[FAIL] No image model chosen")
        raise click.ClickException(
            "Set build.vertex_image_model, or pass --model to try one. "
            "`outreach vertex-models` lists what Google publishes."
        )
    if model:
        from .providers.vertex_image import _strip_model_prefix

        client.model = _strip_model_prefix(model)

    try:
        data, mime = client.generate_image(
            "A soft, out-of-focus warm light gradient. Texture only, no objects, "
            "no people, no text. Muted amber and cream."
        )
    except SiteGenError as exc:
        click.echo(f"[FAIL] {chosen} did not produce an image")
        # The model list is already in hand from the reachability check above.
        # Making him run a second command to see names we already fetched is
        # a round trip for nothing.
        if exc.status_code == 404 and names:
            images = [n for n in names if "image" in n.lower()]
            click.echo("")
            click.echo("       Google publishes these image models:")
            for name in images or names:
                click.echo(f"         {name}")
            click.echo("")
            # The list above is Google's catalogue, not this project's
            # availability in this location -- and that gap is exactly what a
            # 404 on a name that IS in the list means. Image models, preview
            # ones especially, are frequently not served on `global`.
            click.echo(
                f"       That list is Google's catalogue. A 404 on a name that IS in it"
            )
            click.echo(
                f"       usually means the model is not served in region"
                f" '{config.build.vertex_region}'."
            )
            click.echo("       Try another region:")
            for candidate in ("us-central1", "asia-southeast1"):
                if candidate != config.build.vertex_region:
                    click.echo(
                        f"         outreach vertex-check --model {chosen}"
                        f" --region {candidate}"
                    )
        raise click.ClickException(str(exc)) from exc

    out = Path(".cache") / "vertex-check.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    click.echo(f"[ OK ] Image generated with {chosen} ({len(data):,} bytes, {mime})")
    click.echo(f"       {out.resolve()}  -- open it and look at it")
    click.echo("")
    click.echo(
        "That image was paid for with Google Cloud credits, not the AI Studio "
        "free tier. Check Billing -> Reports in a few hours to confirm the "
        "credit line matches the cost."
    )


@main.command("list-models")
@click.pass_context
def list_models(ctx: click.Context) -> None:
    """Ask Gemini which models your key can actually use.

    Model names move -- the name in a config file written months ago will
    one day 404. Rather than guessing, ask the API and paste back one of
    the names it prints.
    """
    import requests

    config = _load(ctx.obj["config_path"])
    key = config.secrets.gemini_api_key
    if not key:
        raise click.ClickException(
            "GEMINI_API_KEY is not set. Add a line to .env:\n"
            "    GEMINI_API_KEY=your-key-here\n"
            "Get one at aistudio.google.com -- the key icon in the left sidebar."
        )

    try:
        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": key},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise click.ClickException(f"Could not reach Gemini: {exc}") from exc

    if resp.status_code == 403:
        raise click.ClickException(
            "Gemini refused the key (403). Check GEMINI_API_KEY in .env is the "
            "one from aistudio.google.com, pasted whole and without quotes."
        )
    if resp.status_code != 200:
        raise click.ClickException(
            f"Gemini returned HTTP {resp.status_code}: {resp.text[:400]}"
        )

    models = [
        m
        for m in (resp.json().get("models") or [])
        if "generateContent" in (m.get("supportedGenerationMethods") or [])
    ]
    if not models:
        raise click.ClickException("Your key sees no models that can generate content.")

    click.echo("")
    click.echo("Models your key can use for building sites:")
    click.echo("")
    for m in sorted(models, key=lambda m: m.get("name", "")):
        # The API returns "models/gemini-3.6-flash"; the config wants the tail.
        name = m.get("name", "").removeprefix("models/")
        limit = m.get("outputTokenLimit")
        note = f"  (max output {limit:,})" if limit else ""
        click.echo(f"  {name}{note}")
    click.echo("")
    click.echo(f"Currently set to: build.model = {config.build.model}")
    click.echo("Change it under `build:` in your campaign file.")


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


@main.command("test-model")
@click.pass_context
def test_model(ctx: click.Context) -> None:
    """Check the configured model can hold every schema the pipeline needs.

    Three calls against one invented business. Worth running whenever you
    change provider or model -- especially the site spec, which is where a
    model that looks fine on the research brief usually falls over.
    """
    from . import modelcheck

    config = _load(ctx.obj["config_path"])
    report = modelcheck.run(config, _writer(config))

    click.echo("")
    for line in report.lines():
        click.echo(line)

    if config.outreach.model:
        click.echo("")
        outreach_report = modelcheck.run_outreach(config, _outreach_writer(config))
        for line in outreach_report.lines():
            click.echo(line)
        if not outreach_report.ok:
            raise SystemExit(1)

    if not report.ok:
        raise SystemExit(1)


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

    # A lead disqualified after its brief was approved must not be moved
    # back to Site Built -- recording a build for someone who asked not to
    # be contacted would put them straight back in the outreach queue.
    leads = sheets.read_rows(schema.LEADS)
    entry = leads.get(lead_id)
    current_stage = (entry[1].get("Stage") if entry else "") or ""
    if current_stage.strip() == Stage.DISQUALIFIED.value:
        raise click.ClickException(
            f"{lead_id} is disqualified -- its site will not be recorded. "
            "If this is wrong, fix the Stage on the Leads tab first."
        )

    sheets.update_column(
        schema.SITE_BRIEFS,
        "Lovable Project ID",
        {lead_id: project_id or "built-via-mcp"},
    )
    if preview_url:
        sheets.update_column(schema.SITE_BRIEFS, "Preview URL", {lead_id: preview_url})
    # advance(), not a bare Stage value: never move a lead backwards.
    sheets.update_column(
        schema.LEADS,
        "Stage",
        {lead_id: advance(current_stage, Stage.SITE_BUILT).value},
    )

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


@main.command("cloudflare-check")
@click.option("--folder", default="", help="A site folder to dry-run the file list for.")
@click.pass_context
def cloudflare_check(ctx: click.Context, folder: str) -> None:
    """Prove the Cloudflare connection before anything is published.

    Checks the token and account id, lists the Pages projects they can see,
    and -- with --folder -- shows exactly which files a publish would upload
    without uploading anything.
    """
    from pathlib import Path

    from .providers.cloudflare import CloudflareClient, CloudflareError, site_files

    config = _load(ctx.obj["config_path"])

    click.echo(f"Campaign config  {ctx.obj['config_path'] or '(default)'}")
    click.echo(
        f"[{'OK' if config.secrets.cloudflare_api_token else '  '}] "
        "CLOUDFLARE_API_TOKEN"
    )
    click.echo(
        f"[{'OK' if config.secrets.cloudflare_account_id else '  '}] "
        "CLOUDFLARE_ACCOUNT_ID"
    )

    if folder:
        # Deliberately first, and offline: a wrong folder is the likeliest
        # mistake and costs nothing to catch here.
        try:
            files = site_files(Path(folder))
        except CloudflareError as exc:
            raise click.ClickException(str(exc))
        click.echo("")
        click.echo(f"Would publish {len(files)} file(s) from {folder}:")
        for path in files[:20]:
            click.echo(f"  {path.relative_to(Path(folder)).as_posix()}")
        if len(files) > 20:
            click.echo(f"  ... and {len(files) - 20} more")

    try:
        client = CloudflareClient(
            config.secrets.cloudflare_api_token,
            config.secrets.cloudflare_account_id,
        )
        projects = client.list_projects()
    except CloudflareError as exc:
        raise click.ClickException(str(exc))
    except Exception as exc:  # noqa: BLE001 -- the raw failure is the point
        raise click.ClickException(f"{type(exc).__name__}: {exc}")

    click.echo("")
    click.echo("[ OK ] Cloudflare answered. Pages projects on this account:")
    for name in projects or ["(none yet -- create one in the Cloudflare dashboard)"]:
        click.echo(f"  {name}")

    wanted = config.build.cloudflare_project
    click.echo("")
    if not wanted:
        click.echo(
            "Set build.cloudflare_project to one of those names, then run "
            "this again."
        )
    elif wanted in projects:
        click.echo(f"[ OK ] build.cloudflare_project is {wanted}, and it exists.")
    else:
        click.echo(
            f"build.cloudflare_project is {wanted!r}, which is not in that "
            "list. Publishing would fail."
        )


@main.command("shot-check")
@click.argument("source", required=False, default="")
@click.pass_context
def shot_check(ctx: click.Context, source: str) -> None:
    """Prove the browser can take a screenshot, without running a draft.

    Give it nothing and it photographs example.com; give it a Preview URL or
    a path to an index.html and it photographs that. Writes .cache/shot.png
    and says exactly which headless mode worked, so a browser that cannot do
    this is a one-command answer rather than a failed batch.
    """
    from pathlib import Path

    from . import screenshot

    config = _load(ctx.obj["config_path"])
    browser = config.build.screenshot_browser
    found = screenshot.find_browser(browser)

    click.echo(f"Browser: {found or '(none found)'}")
    if not found:
        raise click.ClickException(
            "No browser found. Edge or Chrome will do. If one is installed "
            "somewhere unusual, put its full path in build.screenshot_browser."
        )

    target = source or "https://example.com"
    out = Path(".cache/shot.png")
    click.echo(f"Page:    {target}")

    try:
        shot = screenshot.capture(
            target,
            out,
            browser=browser,
            timeout=float(config.build.screenshot_timeout),
        )
    except screenshot.ScreenshotError as exc:
        raise click.ClickException(str(exc))

    size = shot.path.stat().st_size
    click.echo(f"Wrote:   {shot.path} ({size:,} bytes)")
    click.echo("")
    click.echo("Screenshots work. Open that file to check it looks right.")


@main.command("whatsapp-send-test")
@click.argument("number")
@click.option("--text", default="Test from Jarvis. Ignore this.", help="What to send.")
@click.option("--image", default="", help="Optional path to a picture to attach.")
@click.pass_context
def whatsapp_send_test(ctx: click.Context, number: str, text: str, image: str) -> None:
    """Send one message through the real relay and print exactly what came back.

    Aim it at your own number. It uses the same path the dashboard uses --
    the same normalisation, the same relay call -- so whatever it prints is
    what the Send button is doing, without a Sheet or a queue in the way.

        outreach whatsapp-send-test +60123456789
    """
    import json as _json

    from . import phone as phone_lib
    from .providers.whatsapp import WhatsAppClient, WhatsAppError

    config = _load(ctx.obj["config_path"])
    client = WhatsAppClient(port=config.outreach.whatsapp_relay_port)

    status = client.get_status()
    click.echo(f"Relay:    {'connected' if status.get('connected') else 'NOT connected'}")
    if not status.get("connected"):
        click.echo(f"          {_json.dumps(status)[:200]}")
        raise click.ClickException(
            "The relay is not connected. Start Dashboard.bat and scan the QR "
            "code, then run this again."
        )

    home = config.compliance.country_code
    dialled = phone_lib.normalise(number, phone_lib.resolve_country(number, home))
    click.echo(f"Dialling: {dialled or '(could not resolve a country)'}")
    if not dialled:
        raise click.ClickException(
            "Pass the number in full international form, e.g. +60123456789."
        )

    try:
        # country="" -- the number is already E.164, exactly as the dashboard
        # sends it, so this exercises the same path rather than a tidier one.
        result = client.send_message(dialled, text, "", image or None)
    except WhatsAppError as exc:
        click.echo("")
        raise click.ClickException(f"The relay refused it: {exc}")

    click.echo("")
    click.echo(f"Status:   {result.status}")
    click.echo(f"Message:  {result.message_id or '(none returned)'}")
    if result.note:
        click.echo(f"Note:     {result.note}")
    click.echo("")
    if result.status == "sent":
        click.echo("WhatsApp confirmed delivery. Check the chat on your phone.")
    else:
        click.echo(
            "The relay accepted it but WhatsApp has not confirmed yet. Look "
            "for the chat on your phone -- if it is there, sending works and "
            "only the confirmation is slow."
        )


@main.command("whatsapp-probe")
@click.argument("numbers", nargs=-1, required=True)
@click.pass_context
def whatsapp_probe(ctx: click.Context, numbers: tuple[str, ...]) -> None:
    """Ask WhatsApp about one number and show exactly what came back.

    Give it a number you have already found on WhatsApp by hand, in full
    international form: outreach whatsapp-probe +442079460958

    It prints the digits the relay actually asked about and WhatsApp's raw
    reply, so a disagreement between this tool and your phone can be pinned
    on the number, the lookup, or the relay -- rather than guessed at.
    """
    from .providers.whatsapp import WhatsAppClient, WhatsAppError
    from . import phone as phone_lib

    config = _load(ctx.obj["config_path"])
    client = WhatsAppClient(port=config.outreach.whatsapp_relay_port)

    if not client.is_connected():
        raise click.ClickException(
            "The relay is not connected. Start it and pair WhatsApp in the "
            "dashboard, then run this again."
        )

    home = config.compliance.country_code
    dialled = []
    for raw in numbers:
        e164 = phone_lib.normalise(raw, phone_lib.resolve_country(raw, home))
        click.echo(f"{raw}  ->  {e164 or '(could not resolve a country)'}")
        if e164:
            dialled.append(e164)

    if not dialled:
        raise click.ClickException(
            "No usable number. Pass it in full international form, e.g. "
            "+442079460958."
        )

    try:
        data = client.check_raw(dialled)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"The relay call failed: {exc}")

    click.echo("")
    click.echo(json.dumps(data, indent=2)[:4000])
    click.echo("")
    click.echo(
        "registered: true  -> the pipeline will treat this lead as reachable.\n"
        "registered: false -> WhatsApp says no for the digits under 'asked'.\n"
        "                     If those digits are the ones that work on your\n"
        "                     phone, the lookup is at fault, not the number."
    )


@run.command("publish")
@click.option("--limit", type=int, default=0, help="Publish fewer than all of them.")
@click.pass_context
def run_publish(ctx: click.Context, limit: int) -> None:
    """Put approved sites on Cloudflare and record their addresses.

    Reads each site out of your local clone of the repository AI Studio
    pushes to, uploads it, and writes the URL back to the Sheet. It never
    writes to the repository.
    """
    from .providers.cloudflare import CloudflareError
    from .stages import publish as publish_stage

    config = _load(ctx.obj["config_path"])
    sheets = _sheets_client(config)

    try:
        report = publish_stage.run(config, sheets, limit=limit)
    except CloudflareError as exc:
        raise click.ClickException(str(exc))

    click.echo("")
    for line in report.lines():
        click.echo(line)
    click.echo("")


@run.command("reachability")
@click.option("--limit", type=int, default=0, help="Check fewer leads than are waiting.")
@click.option(
    "--recheck",
    is_flag=True,
    help=(
        "Ask again for leads that already have an answer. Needed once for "
        "leads answered before the check asked WhatsApp directly -- every "
        "'no' decided from the number's format alone is untrustworthy, "
        "because WhatsApp Business runs on landlines."
    ),
)
@click.pass_context
def run_reachability(ctx: click.Context, limit: int, recheck: bool) -> None:
    """Stage 3.5 -- who can we actually reach, before a site is built for them.

    Answers from the numbering plan where a country has one, and asks
    WhatsApp only where it does not (the US and Canada, mainly). Cached: a
    lead answered once is never asked again.
    """
    config = _load(ctx.obj["config_path"])
    sheets = _sheets_client(config)
    report = reachability_stage.check_reachability(
        config, sheets, limit=limit, recheck=recheck
    )

    click.echo("")
    for line in report.lines():
        click.echo(line)
    click.echo("")


def _confirm_interactive(prompt: str) -> bool:
    """Ask before spending credits -- but only when someone could answer.

    The dashboard and scheduled runs launch stages as subprocesses with no
    readable stdin. A prompt there is not a question, it is a hang: nothing
    on the page shows it and no keyboard is wired up to answer it. Raising
    a skip instead turns the build into a per-lead skip the report counts,
    and a run from a real terminal still gets the prompt.
    """
    if not sys.stdin or not sys.stdin.isatty():
        raise LovableSkipped(
            "build confirmation needed, but no terminal is attached to answer "
            "it -- run site-build from a terminal, or set "
            "lovable.require_confirmation to false in your campaign"
        )
    return click.confirm(prompt)


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
@click.option(
    "--rewrite",
    is_flag=True,
    help=(
        "Regenerate the prompt for leads that already have a Site Briefs row "
        "(a normal run skips them). Skips any row Lovable has already built "
        "from. Costs one model call per lead, and because the model is "
        "non-deterministic, a prompt you already read and approved will come "
        "back different -- review it again before approving."
    ),
)
@click.pass_context
def run_site_brief(
    ctx: click.Context, limit: int | None, social_first: bool | None, rewrite: bool
) -> None:
    """Stage 4 -- write a ready-to-paste Lovable prompt per researched lead."""
    config = _load(ctx.obj["config_path"])
    sheets = _sheets_client(config)
    lovable = LovableAdapter(
        enabled=config.lovable.enabled,
        require_confirmation=config.lovable.require_confirmation,
        confirm=_confirm_interactive,
    )
    report = site_brief_stage.run(
        config, sheets, _writer(config), lovable,
        limit=limit, social_first=social_first, rewrite=rewrite,
    )

    click.echo("")
    for line in report.summary_lines():
        click.echo(line)
    click.echo("")
    if rewrite:
        click.echo(
            "Rewritten prompts are back in the Site Briefs tab. Approval "
            "Status, Lovable Project ID, Preview URL and Created At are "
            "untouched -- a site you already built keeps its link."
        )
    else:
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
        confirm=_confirm_interactive,
    )
    try:
        report = site_brief_stage.build_approved(config, sheets, lovable, limit=limit)
    except LovableUnavailable as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo("")
    for line in report.summary_lines():
        click.echo(line)
    _echo_sheet_link(config)


@run.command("site-generate")
@click.option("--limit", type=int, default=None, help="Process fewer than the batch size.")
@click.pass_context
def run_site_generate(ctx: click.Context, limit: int | None) -> None:
    """Stage 4c -- write the approved demo pages to disk. Publishes nothing.

    Gemini writes one self-contained HTML file per approved lead into
    `sites/`. Nothing is deployed and nothing is written to the Sheet, so a
    bad batch costs Gemini calls and nothing else. Open the files it prints
    and look at them before anyone else can.
    """
    from .providers.sitegen import SiteGenError, SiteGenerator

    config = _load(ctx.obj["config_path"])
    if not config.secrets.gemini_api_key:
        raise click.ClickException(
            "GEMINI_API_KEY is not set. Create a key at aistudio.google.com "
            "(the key icon in the left sidebar), then paste it into the Model "
            "card in the dashboard -- it is written to .env for you."
        )

    sheets = _sheets_client(config)
    writer = make_writer(config)
    # None on the default "gemini" path -- see `_image_client` for why that
    # matters more than a one-line function suggests.
    image_client = _image_client(config)
    generator = SiteGenerator(
        config.secrets.gemini_api_key,
        model=config.build.model,
        max_output_tokens=config.build.max_output_tokens,
        min_interval_seconds=config.build.min_interval_seconds,
        image_client=image_client,
    )

    # The page call goes to Gemini either way -- GEMINI_API_KEY is required
    # above regardless of image_provider, because it still writes the HTML.
    click.echo(
        f"Writing pages with {config.build.model}. Gemini's free tier allows "
        f"10 calls a minute, so this waits {config.build.min_interval_seconds:.0f}s "
        "between pages -- 10 pages takes about a minute."
    )
    if image_client is not None:
        click.echo(
            f"Images: Vertex, model {image_client.model}, project "
            f"{image_client.project}, region {image_client.region}. At most "
            f"{config.build.max_images_per_run} images this run -- spending "
            "Google Cloud credits, not the free tier."
        )
    try:
        report = site_brief_stage.generate_sites(
            config, sheets, writer, generator, limit=limit
        )
    except SiteGenError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo("")
    for line in report.summary_lines():
        click.echo(line)


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
        config, sheets, _outreach_writer(config), gmail,
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
