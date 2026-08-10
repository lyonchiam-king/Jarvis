"""A local dashboard in your browser.

Answers one question on load: *what should I do today?* -- then gives you the
buttons to do it.

Runs on 127.0.0.1 only. It is not reachable from your network, and it holds no
credentials of its own: it reuses the same token and config as the CLI.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import webbrowser
from collections import deque
import os
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from . import schema
from .config import Config, load_config
from .stage import Stage

log = logging.getLogger("outreach")

# Stage order as it appears in the funnel. Disqualified is deliberately last
# and visually separated -- it's an outcome, not a step.
FUNNEL = [
    Stage.DISCOVERED,
    Stage.RESEARCHED,
    Stage.SITE_BRIEFED,
    Stage.SITE_BUILT,
    Stage.EMAIL_DRAFTED,
    Stage.SENT,
    Stage.REPLIED,
    Stage.CLOSED,
]

STAGE_COMMANDS = {
    "discovery": ["run", "discovery"],
    "research": ["run", "research"],
    "site-brief": ["run", "site-brief"],
    "site-brief-rewrite": ["run", "site-brief", "--rewrite"],
    "site-build": ["run", "site-build"],
    "site-generate": ["run", "site-generate"],
    "outreach-draft": ["run", "outreach-draft"],
    "preflight": ["preflight"],
    "test-model": ["test-model"],
    "preview-leads": ["run", "discovery", "--dry-run"],
    "preview-messages": ["run", "outreach-draft", "--dry-run"],
}

FULL_RUN = ["discovery", "research", "site-brief", "outreach-draft"]

# Stages that accept `--limit N`. `preflight` just checks connections, so a
# number would be meaningless there.
TAKES_LIMIT = {
    "discovery",
    "research",
    "site-brief",
    "site-brief-rewrite",
    "site-build",
    "site-generate",
    "outreach-draft",
    "preview-leads",
    "preview-messages",
}

# Stages that choose which leads to work on, and so honour the socials-first
# preference. `discovery` has no queue to rank -- it is finding them.
RANKS_ITS_QUEUE = {
    "research",
    "site-brief",
    # Rewriting picks a queue too, so the socials-first tick has to reach it.
    # A button that quietly ignores the box next to it is the exact bug that
    # was reported against "Write briefs" -- once is enough.
    "site-brief-rewrite",
    "outreach-draft",
    "preview-messages",
}

# A guard against a slipped digit, not a policy: the campaign's own batch_size
# is the real cap and the CLI enforces it. This only stops a typo becoming a
# very long run.
MAX_LIMIT = 500


def stage_args(
    stage: str, limit: int | None = None, *, social_first: bool | None = None
) -> list[str]:
    """CLI arguments for a stage, with the per-run options applied."""
    args = list(STAGE_COMMANDS[stage])
    if limit and stage in TAKES_LIMIT:
        args += ["--limit", str(limit)]
    # Every stage that picks a queue honours it. Ranking research alone left
    # leads researched before the setting existed sitting at the front of
    # site-brief -- which looks exactly like the checkbox being ignored.
    if social_first is not None and stage in RANKS_ITS_QUEUE:
        args.append("--social-first" if social_first else "--sheet-order")
    return args


@dataclass
class Summary:
    total: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)
    disqualified: int = 0
    to_research: int = 0
    to_brief: int = 0
    to_draft: int = 0
    emails_waiting: int = 0
    whatsapp_waiting: int = 0
    awaiting_approval: int = 0
    approved_unbuilt: int = 0
    errors: int = 0
    errors_retryable: int = 0
    sheet_url: str = ""

    @property
    def has_work(self) -> bool:
        return bool(
            self.to_research
            or self.to_brief
            or self.to_draft
            or self.emails_waiting
            or self.whatsapp_waiting
            or self.awaiting_approval
            or self.approved_unbuilt
        )


def summarise(
    leads: dict[str, tuple[int, dict[str, str]]],
    research_ids: set[str],
    site_briefs: dict[str, tuple[int, dict[str, str]]] | set[str],
    outreach: dict[str, tuple[int, dict[str, str]]],
    errors: dict[str, tuple[int, dict[str, str]]],
    *,
    require_site: bool = False,
) -> Summary:
    """Turn raw Sheet rows into the numbers a founder acts on.

    Pure and side-effect free, so it can be tested without a Sheet.

    ``require_site`` must mirror ``outreach.require_site``: with it on, a lead
    is not "waiting to be drafted" until its demo exists, and a card saying
    otherwise would send you to press a button that does nothing.
    """
    from .stage import parse, rank

    summary = Summary()
    summary.by_stage = {s.value: 0 for s in FUNNEL}
    site_brief_ids = set(site_briefs)

    for lead_id, (_row, row) in leads.items():
        summary.total += 1
        current = parse(row.get("Stage")) or Stage.DISCOVERED

        if current is Stage.DISQUALIFIED:
            summary.disqualified += 1
            continue

        if current.value in summary.by_stage:
            summary.by_stage[current.value] += 1

        # Outstanding work, using the same rules the stages themselves apply.
        if lead_id not in research_ids and rank(current) < rank(Stage.RESEARCHED):
            summary.to_research += 1
        if (
            lead_id not in site_brief_ids
            and rank(Stage.RESEARCHED) <= rank(current) < rank(Stage.SITE_BRIEFED)
        ):
            summary.to_brief += 1
        draft_from = Stage.SITE_BUILT if require_site else Stage.RESEARCHED
        if (
            lead_id not in outreach
            and rank(draft_from) <= rank(current) < rank(Stage.EMAIL_DRAFTED)
        ):
            summary.to_draft += 1

    # Site briefs waiting on you. Approval is a manual column by design, so
    # these two numbers are the human gate in front of Lovable spending.
    if isinstance(site_briefs, dict):
        for _lead_id, (_row, row) in site_briefs.items():
            approval = (row.get("Approval Status") or "").strip().casefold()
            built = bool((row.get("Lovable Project ID") or "").strip())
            if approval == "approved" and not built:
                summary.approved_unbuilt += 1
            elif approval in ("", "pending"):
                summary.awaiting_approval += 1

    for _lead_id, (_row, row) in outreach.items():
        if (row.get("Status") or "").strip().casefold() != "drafted":
            continue
        if (row.get("Channel") or "").strip().casefold() == "email":
            summary.emails_waiting += 1
        else:
            summary.whatsapp_waiting += 1

    for _key, (_row, row) in errors.items():
        summary.errors += 1
        if (row.get("Retryable") or "").strip().upper() == "TRUE":
            summary.errors_retryable += 1

    return summary


def review_queue(
    leads: dict[str, tuple[int, dict[str, str]]],
    research: dict[str, tuple[int, dict[str, str]]],
    site_briefs: dict[str, tuple[int, dict[str, str]]],
) -> list[dict[str, str]]:
    """Briefs waiting for a yes or no, newest work first.

    Each entry carries enough of the research to decide without opening the
    Sheet -- the rating, the angle, what customers said. Deciding blind is how
    you end up approving sixty sites you never looked at.

    Pure, like ``summarise``, so the ordering and the payload can be tested
    without touching Google.
    """
    queue: list[dict[str, str]] = []
    for lead_id, (_n, brief) in site_briefs.items():
        approval = (brief.get("Approval Status") or "").strip().casefold()
        if approval not in ("", "pending"):
            continue
        if (brief.get("Lovable Project ID") or "").strip():
            continue

        lead = leads.get(lead_id, (0, {}))[1]
        found = research.get(lead_id, (0, {}))[1]
        queue.append(
            {
                "lead_id": lead_id,
                "name": brief.get("Business Name") or lead.get("Business Name", ""),
                "category": lead.get("Category", ""),
                "address": lead.get("Address", ""),
                "phone": lead.get("Phone", ""),
                "rating": lead.get("Rating", ""),
                "reviews": lead.get("Review Count", ""),
                "maps_url": lead.get("Maps URL", ""),
                "angle": found.get("Suggested Website Angle", ""),
                "praise": found.get("Customer Praise", ""),
                "pain": found.get("Customer Pain Points", ""),
                "social": found.get("Social Handle", ""),
                "prompt": brief.get("Lovable Prompt", ""),
            }
        )

    from .selection import social_rank

    queue.sort(key=lambda q: social_rank(leads.get(q["lead_id"], (0, {}))[1]))
    return queue


def queue_preview(
    leads: dict[str, tuple[int, dict[str, str]]],
    research_ids: set[str],
    brief_ids: set[str],
    *,
    social_first: bool = True,
    limit: int = 6,
) -> dict[str, list[dict[str, object]]]:
    """Exactly who the next run would pick, per stage.

    Answers the question the counts cannot: *which* businesses, and are they
    the good ones? Without this the ranking is invisible -- you press a button
    and hope -- and the honest reaction to that is to wonder whether you need
    to clear the Sheet first. You do not; this shows why.
    """
    from .selection import select_pending, social_rank

    prefer = social_rank if social_first else None
    queues = {
        "research": (research_ids, Stage.DISCOVERED, Stage.RESEARCHED),
        "site-brief": (brief_ids, Stage.RESEARCHED, Stage.SITE_BRIEFED),
    }

    preview: dict[str, list[dict[str, object]]] = {}
    for name, (done, requires, target) in queues.items():
        selected, _already, _not_ready = select_pending(
            leads, done, requires=requires, target=target,
            batch_size=limit, prefer=prefer,
        )
        preview[name] = [
            {
                "name": row.get("Business Name", "") or row.get("Lead ID", ""),
                "social": row.get("Website Status") == "social_only",
            }
            for row in selected
        ]
    return preview


class Runner:
    """Runs one stage at a time in a subprocess, streaming output to the page."""

    def __init__(self) -> None:
        self.lines: deque[str] = deque(maxlen=500)
        self.running: str | None = None
        self._lock = threading.Lock()

    def start(
        self,
        label: str,
        stages: list[str],
        limit: int | None = None,
        social_first: bool | None = None,
    ) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = label
            self.lines.clear()
        threading.Thread(
            target=self._run, args=(stages, limit, social_first), daemon=True
        ).start()
        return True

    def _run(
        self,
        stages: list[str],
        limit: int | None = None,
        social_first: bool | None = None,
    ) -> None:
        try:
            for stage in stages:
                args = stage_args(stage, limit, social_first=social_first)
                self.lines.append(f"$ outreach {' '.join(args)}")
                process = subprocess.Popen(
                    [sys.executable, "-m", "outreach", *args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    self.lines.append(line.rstrip())
                process.wait()
                if process.returncode != 0:
                    self.lines.append(f"[stopped -- exit code {process.returncode}]")
                    break
        except Exception as exc:  # noqa: BLE001 -- surface it on the page
            self.lines.append(f"[error] {type(exc).__name__}: {exc}")
        finally:
            self.lines.append("")
            self.lines.append("Finished.")
            with self._lock:
                self.running = None


def _sheets_client(config: Config):
    from .auth import get_credentials
    from .sheets import SheetsClient

    creds = get_credentials(config.secrets.oauth_client_file, config.secrets.token_file)
    return SheetsClient(creds, config.secrets.sheet_id)


def _load_summary(config: Config) -> Summary:
    if not config.secrets.sheet_id:
        return Summary()

    sheets = _sheets_client(config)

    summary = summarise(
        sheets.read_rows(schema.LEADS),
        set(sheets.read_rows(schema.RESEARCH)),
        sheets.read_rows(schema.SITE_BRIEFS),
        sheets.read_rows(schema.OUTREACH_LOG),
        sheets.read_rows(schema.ERRORS),
        require_site=config.outreach.require_site,
    )
    summary.sheet_url = sheets.url
    return summary


def create_app(config: Config) -> Flask:
    app = Flask(__name__)
    runner = Runner()

    # The campaign target is editable from the page, so config is state rather
    # than a constant. Every handler reads through `current()` so a save takes
    # effect immediately instead of at the next restart.
    state = {"config": config}

    def current() -> Config:
        return state["config"]

    @app.route("/")
    def index():
        return render_template_string(PAGE, campaign=current().campaign)

    @app.route("/guide")
    def guide():
        from .guide import guide_html

        return render_template_string(GUIDE_PAGE, body=guide_html())

    @app.route("/api/campaign")
    def api_campaign():
        c = current()
        return jsonify(
            {
                "category": c.campaign.category,
                "location": c.campaign.location,
                "areas": list(c.campaign.areas),
                "batch_size": c.batch_size,
                "file": str(c.source_path),
            }
        )

    def _writable_campaign() -> Path:
        """The file to edit: a gitignored local copy, forked on first write.

        The shipped campaign file is tracked by git, so writing to it means
        every `git pull` after a settings change stops with "your local
        changes would be overwritten". Forking once and pointing CAMPAIGN_CONFIG
        at the copy ends that for good -- the tracked file stays the template.
        """
        from .campaign_edit import fork_to_local
        from .env_edit import set_key

        current_path = current().source_path
        local = fork_to_local(current_path)
        if local != current_path:
            set_key(Path(".env"), "CAMPAIGN_CONFIG", str(local))
            os.environ["CAMPAIGN_CONFIG"] = str(local)
            log.info("Settings now live in %s, so git pull stays clean.", local)
        return local

    @app.route("/api/model")
    def api_model():
        """The model in use, and whether its key is present. Never the key."""
        from .config import PROVIDER_DEFAULTS, PROVIDER_KEY_ENV
        from .env_edit import is_set

        c = current()
        env_name = PROVIDER_KEY_ENV.get(c.research.provider, "")
        return jsonify(
            {
                "provider": c.research.provider,
                "model": c.research.model,
                "base_url": c.research.base_url,
                "providers": sorted(PROVIDER_DEFAULTS),
                "key_env": env_name,
                "key_needed": c.research.provider != "ollama",
                "key_set": bool(env_name) and is_set(Path(".env"), env_name),
                "guaranteed": c.research.provider == "anthropic",
            }
        )

    @app.route("/api/model", methods=["POST"])
    def api_set_model():
        """Change provider/model, and optionally store that provider's key.

        The key is write-only. It goes into .env and is never read back to the
        page -- the browser only ever learns whether one is set.
        """
        from .campaign_edit import CampaignEditError, rewrite_research  # noqa: F401
        from .config import PROVIDER_DEFAULTS, PROVIDER_KEY_ENV, ConfigError, load_config
        from .env_edit import EnvEditError, set_key

        payload = request.json or {}
        provider = (payload.get("provider") or "").strip().lower() or None
        # Absent means "leave it alone"; present but blank is a mistake worth
        # naming, since the field is pre-filled and clearing it looks deliberate.
        model = payload.get("model")
        if model is not None:
            model = model.strip()
            if not model:
                return jsonify({"error": "Model cannot be empty -- name the model to use."}), 200
        base_url = payload.get("base_url")
        api_key = payload.get("api_key") or ""

        if provider is not None and provider not in PROVIDER_DEFAULTS:
            return jsonify({"error": f"Unknown provider {provider!r}"}), 400

        # The key first: if this fails, the campaign file is still untouched.
        if api_key.strip():
            target = provider or current().research.provider
            env_name = PROVIDER_KEY_ENV.get(target)
            if not env_name:
                return jsonify({"error": f"{target} takes no API key"}), 400
            try:
                set_key(Path(".env"), env_name, api_key.strip())
                os.environ[env_name] = api_key.strip()
            except EnvEditError as exc:
                return jsonify({"error": str(exc)}), 200

        try:
            path = _writable_campaign()
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        try:
            original = path.read_text()
            updated = rewrite_research(
                original, provider=provider, model=model, base_url=base_url
            )
            path.write_text(updated)
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        except OSError as exc:
            return jsonify({"error": f"Could not save {path}: {exc}"}), 200

        try:
            state["config"] = load_config(path)
        except ConfigError as exc:
            path.write_text(original)
            return jsonify({"error": f"Reverted -- {exc}"}), 200

        return jsonify({"ok": True})

    @app.route("/api/campaign", methods=["POST"])
    def api_set_campaign():
        """Change what the pipeline hunts for, from the page.

        Writes the campaign YAML in place, preserving its comments, then
        reloads. Nothing already on the Sheet is touched -- changing category
        starts a new hunt alongside the old one, it does not reset anything.
        """
        from .campaign_edit import CampaignEditError, rewrite_campaign  # noqa: F401
        from .config import ConfigError, load_config

        payload = request.json or {}
        category = payload.get("category")
        location = payload.get("location")
        areas = payload.get("areas")
        if areas is not None and not isinstance(areas, list):
            return jsonify({"error": "areas must be a list of strings"}), 400

        raw_size = payload.get("batch_size")
        batch_size: int | None = None
        if raw_size not in (None, ""):
            try:
                batch_size = int(raw_size)
            except (TypeError, ValueError):
                return jsonify({"error": "Batch size must be a whole number"}), 400

        try:
            path = _writable_campaign()
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        try:
            original = path.read_text()
            updated = rewrite_campaign(
                original,
                category=category,
                location=location,
                areas=[str(a) for a in areas] if areas is not None else None,
                batch_size=batch_size,
            )
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        except OSError as exc:
            return jsonify({"error": f"Could not read {path}: {exc}"}), 200

        try:
            path.write_text(updated)
        except OSError as exc:
            return jsonify({"error": f"Could not save {path}: {exc}"}), 200

        try:
            state["config"] = load_config(path)
        except ConfigError as exc:
            # Put the file back rather than leave the user with a campaign the
            # pipeline cannot load. rewrite_campaign validates, so reaching
            # here means something outside the target went wrong.
            path.write_text(original)
            return jsonify({"error": f"Reverted -- {exc}"}), 200

        c = current()
        return jsonify(
            {
                "ok": True,
                "category": c.campaign.category,
                "location": c.campaign.location,
                "areas": list(c.campaign.areas),
                "batch_size": c.batch_size,
            }
        )

    @app.route("/api/summary")
    def api_summary():
        c = current()
        # Distinguish "set up but empty" from "not set up yet". Without this
        # the page says "no leads yet, press Find leads" at someone whose keys
        # aren't filled in, and the button they press just fails.
        missing = [
            name
            for name, value in (
                ("SHEET_ID", c.secrets.sheet_id),
                ("PLACES_API_KEY", c.secrets.places_api_key),
                ("ANTHROPIC_API_KEY", c.secrets.anthropic_api_key),
            )
            if not value
        ]
        if missing:
            return jsonify({"setup_needed": missing})

        try:
            summary = _load_summary(c)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify(
            {
                "total": summary.total,
                "by_stage": summary.by_stage,
                "disqualified": summary.disqualified,
                "to_research": summary.to_research,
                "to_brief": summary.to_brief,
                "to_draft": summary.to_draft,
                "emails_waiting": summary.emails_waiting,
                "whatsapp_waiting": summary.whatsapp_waiting,
                "awaiting_approval": summary.awaiting_approval,
                "approved_unbuilt": summary.approved_unbuilt,
                "errors": summary.errors,
                "errors_retryable": summary.errors_retryable,
                "sheet_url": summary.sheet_url,
                "has_work": summary.has_work,
            }
        )

    @app.route("/api/next-up")
    def api_next_up():
        """Who the next run would work on, so the ranking is visible."""
        c = current()
        if not c.secrets.sheet_id:
            return jsonify({"queues": {}})
        try:
            sheets = _sheets_client(c)
            preview = queue_preview(
                sheets.read_rows(schema.LEADS),
                set(sheets.read_rows(schema.RESEARCH)),
                set(sheets.read_rows(schema.SITE_BRIEFS)),
                social_first=c.research.social_first,
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({"queues": preview})

    @app.route("/api/review-queue")
    def api_review_queue():
        """Briefs waiting on your yes or no, with the research to judge them."""
        if not current().secrets.sheet_id:
            return jsonify({"queue": []})
        try:
            sheets = _sheets_client(current())
            queue = review_queue(
                sheets.read_rows(schema.LEADS),
                sheets.read_rows(schema.RESEARCH),
                sheets.read_rows(schema.SITE_BRIEFS),
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({"queue": queue})

    @app.route("/api/regenerate", methods=["POST"])
    def api_regenerate():
        """Rewrite one brief you are looking at, optionally with your notes.

        Two shapes, one endpoint. With no `notes` it is a plain re-roll: the
        model is not deterministic, so asking again genuinely gives a
        different take rather than the same brief back. With `notes` your
        words are handed to the model alongside the research.

        Notes are deliberately not stored. A "make it darker" that silently
        persisted would still be steering the tenth brief you wrote long after
        you had forgotten typing it.

        Never touches Approval Status. Rewriting is not deciding.
        """
        from .providers.llm import make_writer, render_lovable_prompt
        from .stages.site_brief import regenerate_one

        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        notes = (payload.get("notes") or "").strip()

        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400
        if len(notes) > 2000:
            return jsonify({"error": "notes must be under 2000 characters"}), 400

        try:
            config = current()
            sheets = _sheets_client(config)
            prompt = regenerate_one(
                config, sheets, make_writer(config), lead_id, notes=notes
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({"ok": True, "prompt": prompt})

    @app.route("/api/decide", methods=["POST"])
    def api_decide():
        """Record Approve or Skip against one brief.

        This is the human gate in front of every Lovable credit. It writes only
        the Approval Status cell -- approving does not build anything, it moves
        the row into the build queue where a second, explicit action spends.
        """
        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        decision = (payload.get("decision") or "").strip().casefold()

        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400
        if decision not in ("approved", "skip"):
            return jsonify({"error": "decision must be 'approved' or 'skip'"}), 400

        try:
            sheets = _sheets_client(current())
            written = sheets.update_column(
                schema.SITE_BRIEFS,
                "Approval Status",
                {lead_id: "Approved" if decision == "approved" else "Skip"},
                # Approval Status is a manual column so no *stage* can set it.
                # This is not a stage: it is the user pressing Approve on one
                # named row, which is the whole point of the column existing.
                user_edit=True,
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200

        if not written:
            return jsonify({"error": f"No Site Briefs row for {lead_id}"}), 200
        return jsonify({"ok": True})

    @app.route("/api/build-queue")
    def api_build_queue():
        """Approved briefs waiting to be built, prompt included."""
        if not current().secrets.sheet_id:
            return jsonify({"queue": []})
        try:
            sheets = _sheets_client(current())
            rows = sheets.read_rows(schema.SITE_BRIEFS)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200

        queue = [
            {
                "lead_id": lead_id,
                "name": row.get("Business Name", ""),
                "prompt": row.get("Lovable Prompt", ""),
            }
            for lead_id, (_n, row) in rows.items()
            if (row.get("Approval Status") or "").strip().casefold() == "approved"
            and not (row.get("Lovable Project ID") or "").strip()
        ]
        return jsonify({"queue": queue})

    @app.route("/api/record-build", methods=["POST"])
    def api_record_build():
        """Record a site you built by hand, and move the lead to Site Built.

        Closing this loop matters beyond bookkeeping: Stage 5 puts the preview
        URL in the outreach email, and with require_site on, a lead that is
        never marked built is never written to at all.
        """
        from .stage import Stage

        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        preview_url = (payload.get("preview_url") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400
        if preview_url and not preview_url.startswith(("http://", "https://")):
            return jsonify({"error": "preview URL must start with http"}), 400

        try:
            sheets = _sheets_client(current())
            sheets.update_column(
                schema.SITE_BRIEFS, "Lovable Project ID", {lead_id: "built-by-hand"}
            )
            if preview_url:
                sheets.update_column(
                    schema.SITE_BRIEFS, "Preview URL", {lead_id: preview_url}
                )
            sheets.update_column(
                schema.LEADS, "Stage", {lead_id: Stage.SITE_BUILT.value}
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({"ok": True})

    @app.route("/api/run", methods=["POST"])
    def api_run():
        payload = request.json or {}
        what = payload.get("what", "")

        raw = payload.get("limit")
        limit: int | None = None
        if raw not in (None, ""):
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                return jsonify({"error": "How many must be a whole number"}), 400
            if limit < 1:
                return jsonify({"error": "How many must be at least 1"}), 400
            if limit > MAX_LIMIT:
                return jsonify({"error": f"How many cannot exceed {MAX_LIMIT}"}), 400

        social_first = payload.get("social_first")
        if social_first is not None:
            social_first = bool(social_first)

        if what == "full":
            started = runner.start("Full pipeline", FULL_RUN, limit, social_first)
        elif what in STAGE_COMMANDS:
            started = runner.start(what, [what], limit, social_first)
        else:
            return jsonify({"error": "unknown action"}), 400
        if not started:
            return jsonify({"error": f"{runner.running} is already running"}), 409
        return jsonify({"ok": True})

    @app.route("/api/log")
    def api_log():
        return jsonify({"running": runner.running, "lines": list(runner.lines)})

    return app


def serve(config_path: str | None = None, *, port: int = 8765, open_browser: bool = True):
    config = load_config(config_path)
    app = create_app(config)
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n  Dashboard running at {url}")
    print("  Close this window when you're done.\n")
    # 127.0.0.1 only -- never exposed to the network.
    app.run(host="127.0.0.1", port=port, debug=False)


GUIDE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Handbook &middot; SME Outreach</title>
<style>
  :root {
    --bg:#f6f5f2; --card:#fff; --ink:#1b1a17; --muted:#6b6862;
    --line:#e3e0d8; --accent:#b4552d;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16151a; --card:#1f1e24; --ink:#ece9e3; --muted:#9a958c;
            --line:#2f2d35; --accent:#e07a4e; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:16px/1.65 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width:720px; margin:0 auto; padding:24px 20px 90px; }
  .back { display:inline-block; margin-bottom:26px; font-size:13.5px;
          color:var(--accent); text-decoration:none; }
  h1 { font-size:27px; letter-spacing:-.015em; margin:0 0 20px; }
  h2 { font-size:21px; margin:44px 0 12px; letter-spacing:-.01em;
       padding-top:14px; border-top:1px solid var(--line); }
  h3 { font-size:17px; margin:30px 0 8px; }
  h4 { font-size:15px; margin:22px 0 6px; color:var(--muted); }
  p, li { margin:0 0 13px; }
  ul, ol { padding-left:22px; margin:0 0 16px; }
  a { color:var(--accent); }
  code { font:13.5px/1.5 ui-monospace,Menlo,Consolas,monospace;
         background:var(--card); border:1px solid var(--line);
         border-radius:5px; padding:1px 5px; word-break:break-word; }
  pre { background:#0e0d11; color:#dcd7cf; padding:15px 17px; border-radius:10px;
        overflow-x:auto; font-size:13px; line-height:1.6; margin:0 0 18px; }
  pre code { background:none; border:0; padding:0; color:inherit;
             white-space:pre; word-break:normal; }
  blockquote { margin:0 0 18px; padding:13px 17px; background:var(--card);
               border-left:3px solid var(--accent); border-radius:0 9px 9px 0; }
  blockquote > :last-child { margin-bottom:0; }
  .scroll { overflow-x:auto; margin:0 0 18px; }
  table { border-collapse:collapse; width:100%; font-size:14.5px; }
  th, td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line);
           vertical-align:top; }
  th { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
       color:var(--muted); }
  hr { border:0; border-top:1px solid var(--line); margin:34px 0; }
  h1:target, h2:target, h3:target { scroll-margin-top:18px; }
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/">&lsaquo; Back to the dashboard</a>
  {{ body|safe }}
</div>
<script>
// Wide tables scroll on their own rather than pushing the page sideways.
document.querySelectorAll('table').forEach(t => {
  const box = document.createElement('div');
  box.className = 'scroll';
  t.parentNode.insertBefore(box, t);
  box.appendChild(t);
});
</script>
</body>
</html>
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SME Outreach</title>
<style>
  :root {
    --bg:#f6f5f2; --card:#fff; --ink:#1b1a17; --muted:#6b6862;
    --line:#e3e0d8; --accent:#b4552d; --ok:#2f7d52; --warn:#9a6b1e;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16151a; --card:#1f1e24; --ink:#ece9e3; --muted:#9a958c;
            --line:#2f2d35; --accent:#e07a4e; --ok:#5fbf88; --warn:#d9a441; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width:1040px; margin:0 auto; padding:28px 20px 60px; }
  header { display:flex; justify-content:space-between; align-items:baseline;
           flex-wrap:wrap; gap:10px; margin-bottom:6px; }
  h1 { font-size:22px; margin:0; letter-spacing:-.01em; }
  .sub { color:var(--muted); font-size:13px; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.09em;
       color:var(--muted); margin:30px 0 12px; font-weight:600; }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:12px; padding:18px; }
  .todo { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; }
  .todo .card { display:flex; flex-direction:column; gap:10px; }
  .n { font-size:30px; font-weight:650; letter-spacing:-.02em; line-height:1; }
  .n.zero { color:var(--muted); font-weight:400; }
  .what { font-size:14px; }
  button { font:inherit; font-size:14px; padding:9px 14px; border-radius:8px;
           border:1px solid var(--line); background:transparent; color:var(--ink);
           cursor:pointer; }
  button:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
  button:disabled { opacity:.45; cursor:default; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.primary:hover:not(:disabled) { opacity:.9; color:#fff; }
  .funnel { display:flex; flex-wrap:wrap; gap:8px; }
  .step { flex:1 1 108px; border:1px solid var(--line); border-radius:9px;
          padding:11px 12px; background:var(--card); }
  .step .c { font-size:21px; font-weight:600; }
  .step .l { font-size:11.5px; color:var(--muted); text-transform:uppercase;
             letter-spacing:.05em; margin-top:2px; }
  .step.empty .c { color:var(--muted); font-weight:400; }
  .row { display:flex; gap:9px; flex-wrap:wrap; align-items:center; }
  a { color:var(--accent); }
  pre { background:#0e0d11; color:#dcd7cf; padding:15px; border-radius:10px;
        overflow:auto; max-height:340px; font-size:12.5px; line-height:1.5;
        margin:0; white-space:pre-wrap; word-break:break-word; }
  .hint { color:var(--muted); font-size:13px; margin:9px 0 0; }
  .q { margin-bottom:12px; }
  .q h3 { margin:0 0 10px; font-size:16px; }
  textarea { display:block; width:100%; height:120px;
             font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace;
             background:var(--bg); color:var(--ink); border:1px solid var(--line);
             border-radius:8px; padding:11px; resize:vertical; }
  input { font:inherit; font-size:13.5px; padding:8px 11px;
          border-radius:8px; border:1px solid var(--line);
          background:var(--bg); color:var(--ink); }
  .q input { flex:1 1 260px; }
  label { display:block; }
  .ok { color:var(--ok); font-size:13px; }
  .status { font-size:13px; color:var(--warn); }
  .err { color:var(--accent); }
  .stepno { display:inline-flex; align-items:center; justify-content:center;
            width:19px; height:19px; border-radius:50%; background:var(--accent);
            color:#fff; font-size:11.5px; font-weight:700; margin-right:7px;
            vertical-align:1px; letter-spacing:0; }
  summary { cursor:pointer; }
  .counter { font-size:13px; color:var(--muted); }
  .facts { display:flex; flex-wrap:wrap; gap:6px 14px; margin:2px 0 12px;
           font-size:13px; color:var(--muted); }
  .note { margin:0 0 9px; font-size:14px; }
  .note b { font-weight:600; color:var(--ink); }
  button.approve { background:var(--ok); border-color:var(--ok); color:#fff; }
  button.approve:hover:not(:disabled) { opacity:.9; color:#fff; }
  .sendrow { display:flex; justify-content:space-between; align-items:center;
             gap:12px; padding:9px 0; border-bottom:1px solid var(--line); }
  .sendrow:last-child { border-bottom:0; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>SME Outreach</h1>
    <div class="sub" id="who">{{ campaign.category }} &middot; {{ campaign.location }}</div>
  </header>
  <div class="row" style="justify-content:space-between;align-items:baseline">
    <div class="sub" id="totals">Loading&hellip;</div>
    <a class="sub" href="/guide" target="_blank" rel="noopener"><b>Handbook &rsaquo;</b></a>
  </div>
  <h2>What you're hunting</h2>
  <div class="card" id="target">Loading&hellip;</div>

  <h2>Model</h2>
  <div class="card" id="model">Loading&hellip;</div>

  <h2>What needs you today</h2>
  <div class="todo" id="todo"></div>

  <h2><span class="stepno">1</span> Gather
      <span class="sub" style="text-transform:none;letter-spacing:0">
      &mdash; finds new businesses, researches them, writes their briefs</span>
      <a class="sub" href="/guide#1-press-run-this-mornings-batch" target="_blank" rel="noopener">What is this?</a></h2>
  <div class="card">
    <div class="row">
      <button class="primary" data-run="full">Run this morning's batch</button>
      <label for="howmany" class="sub" style="display:inline">How many</label>
      <input id="howmany" type="number" min="1" step="1" style="width:82px"
             placeholder="all">
      <span class="status" id="running"></span>
    </div>
    <p class="hint">
      Leave <b>How many</b> blank for the full batch, or type a number to do
      fewer &mdash; it applies to whichever button you press. Every run skips
      businesses it has already seen, so running twice is free.
    </p>
    <label class="hint" style="display:flex;gap:8px;align-items:flex-start;margin-top:2px">
      <input type="checkbox" id="socialfirst" checked style="margin-top:3px;width:auto">
      <span><b>Work through the ones with a Facebook or Instagram page first.</b>
      They have more for us to work with, so the brief and the demo come out
      better &mdash; and they have already decided they want to be found online.
      Applies to researching, briefing and drafting alike, so a lead does not
      jump the queue later. With <b>How many</b> set, this is what your budget
      gets spent on.</span>
    </label>
    <details style="margin-top:12px">
      <summary class="sub">Run one step on its own</summary>
      <div class="row" style="margin-top:10px">
        <button data-run="discovery">Find leads</button>
        <button data-run="research">Research</button>
        <button data-run="site-brief">Write briefs</button>
        <button data-run="site-build">Build approved</button>
        <button data-run="outreach-draft">Draft messages</button>
      </div>
      <p class="hint"><b>How many</b> above applies to these too &mdash; set it to
        5, press <b>Research</b>, and only five leads are researched.</p>
      <div class="hint" id="nextup"></div>
      <div class="row" style="margin-top:10px">
        <button data-run="preview-leads">Preview leads</button>
        <button data-run="preview-messages">Preview messages</button>
        <button data-run="preflight">Check setup</button>
        <button data-run="test-model">Test the model</button>
      </div>
      <div class="row" style="margin-top:10px">
        <button data-run="site-brief-rewrite">Rewrite existing briefs</button>
      </div>
      <p class="hint">For briefs you already have &mdash; not new ones.
        Regenerates the Lovable prompt from the current template, skipping
        anything already built. Costs a model call per lead, same as
        <b>Write briefs</b>, and the result is not the same brief with
        improvements &mdash; it is a freshly written one, so a prompt you
        already read and approved will read differently. Re-review it before
        approving again.</p>
    </details>
  </div>

  <h2><span class="stepno">2</span> Review
      <span class="sub" style="text-transform:none;letter-spacing:0">
      &mdash; one at a time. Approve sends it to the build queue; nothing is spent yet</span>
      <a class="sub" href="/guide#2-review-the-briefs-one-at-a-time" target="_blank" rel="noopener">What is this?</a></h2>
  <div id="review"></div>

  <h2><span class="stepno">3</span> Build
      <span class="sub" style="text-transform:none;letter-spacing:0">
      &mdash; approved, waiting for you to paste into Lovable</span>
      <a class="sub" href="/guide#3-build-the-ones-you-approved" target="_blank" rel="noopener">What is this?</a></h2>
  <div class="row" style="margin-bottom:10px">
    <button data-run="site-generate">Generate pages with Gemini</button>
  </div>
  <p class="hint">Writes each approved brief to a file in your
    <code>sites\\</code> folder using Gemini. Nothing is published and nothing
    is sent &mdash; the run prints the path of every page so you can open one
    and look at it first. A page that comes back broken is refused and never
    written. Gemini's free tier allows 10 pages a minute, so 60 takes about
    seven minutes, mostly waiting. Pages already written are skipped and
    cost nothing.</p>
  <div id="queue"></div>

  <h2><span class="stepno">4</span> Send
      <a class="sub" href="/guide#4-open-gmail-drafts" target="_blank" rel="noopener">What is this?</a></h2>
  <div class="card" id="send"></div>

  <h2>Pipeline</h2>
  <div class="funnel" id="funnel"></div>

  <h2>Output</h2>
  <pre id="log">Nothing running.</pre>

</div>
<script>
const $ = s => document.querySelector(s);

function card(n, label, note) {
  return `<div class="card">
    <div class="n ${n ? '' : 'zero'}">${n}</div>
    <div class="what">${label}</div>
    <div class="sub">${note}</div></div>`;
}

async function refresh() {
  let d;
  try { d = await (await fetch('/api/summary')).json(); } catch { return; }

  if (d.error) {
    $('#totals').innerHTML = `<span class="err">${esc(d.error)}</span>`;
    return;
  }

  if (d.setup_needed) {
    $('#totals').innerHTML = '<span class="err">Not set up yet</span>';
    $('#todo').innerHTML = `<div class="card" style="grid-column:1/-1">
      <div class="what"><b>Before this can run, three keys need filling in.</b></div>
      <div class="sub" style="margin:8px 0">
        Still missing from your <code>.env</code> file:
        <b>${d.setup_needed.join('</b>, <b>')}</b></div>
      <div class="sub">The <a href="/guide#getting-it-onto-your-computer"
        target="_blank" rel="noopener">handbook</a> shows where each one comes
        from. Fill them in, save, then reload this page.</div></div>`;
    $('#funnel').innerHTML = '';
    $('#send').innerHTML = '<div class="sub">Nothing yet.</div>';
    return;
  }
  $('#totals').innerHTML = d.total
    ? `${d.total} leads &middot; ${d.disqualified} disqualified &middot;
       <a href="${d.sheet_url}" target="_blank" rel="noopener">Open Sheet</a>`
    : 'No leads yet &mdash; press <b>Find leads</b> to start.';

  // Ordered the way the morning runs: review, build, send, then housekeeping.
  const todo = [];
  if (d.awaiting_approval) todo.push(card(d.awaiting_approval, 'briefs to review',
    'Step 2 below &mdash; Approve or Skip'));
  if (d.approved_unbuilt) todo.push(card(d.approved_unbuilt, 'sites to build',
    'Step 3 below'));
  if (d.emails_waiting) todo.push(card(d.emails_waiting, 'emails to send',
    'Open Gmail drafts, read, press send'));
  if (d.whatsapp_waiting) todo.push(card(d.whatsapp_waiting, 'WhatsApp to send',
    'Copy from the Outreach Log tab'));
  if (d.to_research) todo.push(card(d.to_research, 'leads to research',
    'About 6&cent; each'));
  if (d.to_brief) todo.push(card(d.to_brief, 'need briefs written', 'Free to generate'));
  if (d.to_draft) todo.push(card(d.to_draft, 'need messages drafted', 'Nothing sends'));
  if (d.errors) todo.push(card(d.errors, 'errors logged',
    `${d.errors_retryable} may fix themselves on a re-run`));
  $('#todo').innerHTML = todo.length ? todo.join('')
    : '<div class="card"><div class="what">Nothing waiting.</div>' +
      '<div class="sub">Press <b>Run this morning\\u2019s batch</b> for a fresh set, ' +
      'or add areas to your campaign file.</div></div>';

  $('#funnel').innerHTML = Object.entries(d.by_stage).map(([k, v]) =>
    `<div class="step ${v ? '' : 'empty'}"><div class="c">${v}</div>
     <div class="l">${k}</div></div>`).join('');

  const send = [];
  if (d.emails_waiting) send.push(`<div class="sendrow">
    <div><b>${d.emails_waiting}</b> email${d.emails_waiting > 1 ? 's' : ''} drafted
    <div class="sub">Read each one, check the demo link opens, press send</div></div>
    <a href="https://mail.google.com/mail/u/0/#drafts" target="_blank" rel="noopener">
      <button>Open Gmail drafts</button></a></div>`);
  if (d.whatsapp_waiting) send.push(`<div class="sendrow">
    <div><b>${d.whatsapp_waiting}</b> WhatsApp message${d.whatsapp_waiting > 1 ? 's' : ''}
    <div class="sub">Copy each from the Outreach Log tab, then mark it Sent</div></div>
    <a href="${d.sheet_url}" target="_blank" rel="noopener">
      <button>Open Sheet</button></a></div>`);
  $('#send').innerHTML = send.length ? send.join('')
    : '<div class="what">Nothing to send yet.</div>' +
      '<div class="sub">Messages are written once a lead\\u2019s demo site is built.</div>';
}

async function poll() {
  let d;
  try { d = await (await fetch('/api/log')).json(); } catch { return; }
  if (d.lines.length) $('#log').textContent = d.lines.join('\\n');
  $('#log').scrollTop = $('#log').scrollHeight;
  const busy = !!d.running;
  $('#running').textContent = busy ? `Running ${d.running}\\u2026` : '';
  document.querySelectorAll('button[data-run]').forEach(b => b.disabled = busy);
  if (!busy && poll.wasBusy) loadModel();
  if (!busy && poll.wasBusy) { refresh(); loadNextUp(); }
  poll.wasBusy = busy;
}

function bindRunButtons() {
  document.querySelectorAll('button[data-run]:not([data-bound])').forEach(b => {
    b.dataset.bound = '1';
    b.addEventListener('click', async () => {
      const r = await fetch('/api/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          what: b.dataset.run,
          limit: $('#howmany') ? $('#howmany').value.trim() : '',
          social_first: $('#socialfirst') ? $('#socialfirst').checked : null,
        })
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        $('#running').innerHTML = `<span class="err">${esc(d.error || 'Could not start')}</span>`;
        return;
      }
      poll();
    });
  });
}
bindRunButtons();

// ------------------------------------------------------------------- model

let modelInfo = null;

async function loadModel() {
  try { modelInfo = await (await fetch('/api/model')).json(); } catch { return; }
  drawModel();
}

function drawModel(editing) {
  const el = $('#model');
  const m = modelInfo;
  if (!el || !m) return;

  const keyState = !m.key_needed
    ? '<span class="ok">no key needed</span>'
    : m.key_set
      ? `<span class="ok">${esc(m.key_env)} is set</span>`
      : `<span class="err">${esc(m.key_env)} is missing</span>`;

  if (!editing) {
    el.innerHTML = `<div class="row" style="justify-content:space-between">
      <div>
        <div style="font-size:19px;font-weight:600">${esc(m.model)}</div>
        <div class="sub">via ${esc(m.provider)} &middot; ${keyState}</div>
      </div>
      <div class="row">
        <button data-run="test-model">Test it</button>
        <button onclick="drawModel(true)">Change</button>
      </div>
    </div>
    ${m.guaranteed ? '' : `<p class="hint" style="margin-top:10px">
      Only Anthropic guarantees the reply fits the schema. Press
      <b>Test it</b> after any change &mdash; three calls, a few cents, and it
      tells you whether this model can hold all nineteen fields the site brief
      needs.</p>`}`;
    bindRunButtons();
    return;
  }

  const options = m.providers.map(p =>
    `<option value="${esc(p)}" ${p === m.provider ? 'selected' : ''}>${esc(p)}</option>`
  ).join('');

  el.innerHTML = `
    <label class="sub" for="m-prov">Where it runs</label>
    <select id="m-prov" style="width:100%;margin:5px 0 14px;font:inherit;padding:8px 11px;
      border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink)">
      ${options}</select>
    <label class="sub" for="m-model">Model</label>
    <input id="m-model" style="width:100%;margin:5px 0 4px" value="${esc(m.model)}"
           placeholder="claude-opus-5">
    <p class="hint" style="margin:0 0 14px">e.g. <code>kimi-k2.6</code> on
      ollama-cloud, <code>moonshotai/kimi-k2-thinking</code> on openrouter,
      <code>gemini-2.5-flash</code> on gemini, <code>claude-opus-5</code> on
      anthropic.</p>
    <label class="sub" for="m-key">API key <span id="m-keyenv"></span></label>
    <input id="m-key" type="password" autocomplete="off" style="width:100%;margin:5px 0 4px"
           placeholder="leave blank to keep the current one">
    <p class="hint" style="margin:0 0 14px">Saved into your <code>.env</code>. It is
      never shown again and never sent back to this page &mdash; the dashboard
      only remembers whether one is set.</p>
    <label class="sub" for="m-url">Endpoint <span class="sub">(optional)</span></label>
    <input id="m-url" style="width:100%;margin:5px 0 4px" value="${esc(m.base_url || '')}"
           placeholder="leave blank for the provider default">
    <p class="hint">Only needed for a self-hosted box, e.g.
      <code>http://192.168.1.10:11434/v1</code>.</p>
    <div class="row" style="margin-top:10px">
      <button class="primary" onclick="saveModel()">Save</button>
      <button onclick="drawModel(false)">Cancel</button>
      <span class="sub" id="m-msg"></span>
    </div>`;
}

async function saveModel() {
  const body = {
    provider: $('#m-prov').value,
    model: $('#m-model').value,
    base_url: $('#m-url').value,
    api_key: $('#m-key').value,
  };
  document.querySelectorAll('#model button').forEach(b => b.disabled = true);
  $('#m-msg').textContent = 'Saving…';

  let d;
  try {
    d = await (await fetch('/api/model', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    })).json();
  } catch { d = {error: 'Could not reach the dashboard'}; }

  if (!d.ok) {
    $('#m-msg').innerHTML = `<span class="err">${esc(d.error || 'Failed')}</span>`;
    document.querySelectorAll('#model button').forEach(b => b.disabled = false);
    return;
  }
  await loadModel();
}

// --------------------------------------------------- who is next in the queue

async function loadNextUp() {
  let d;
  try { d = await (await fetch('/api/next-up')).json(); } catch { return; }
  const el = $('#nextup');
  if (!el || d.error || !d.queues) { if (el) el.innerHTML = ''; return; }

  const label = {research: 'Research', 'site-brief': 'Write briefs'};
  const parts = [];
  for (const stage of ['research', 'site-brief']) {
    const rows = d.queues[stage] || [];
    if (!rows.length) continue;
    const names = rows.map(r =>
      esc(r.name) + (r.social ? ' <b title="has a Facebook or Instagram page">&#9733;</b>' : '')
    ).join(', ');
    parts.push(`<div style="margin-top:6px"><b>${label[stage]} would take:</b> ${names}</div>`);
  }
  el.innerHTML = parts.length
    ? parts.join('') +
      '<div style="margin-top:4px">&#9733; has a social page, so it goes first. ' +
      'Older leads without one wait their turn &mdash; nothing needs deleting.</div>'
    : '';
}

// -------------------------------------------------------- what you're hunting

let target = null;

async function loadTarget() {
  try { target = await (await fetch('/api/campaign')).json(); } catch { return; }
  drawTarget();
}

function drawTarget(editing) {
  const el = $('#target');
  if (!target) return;
  if (target.error) { el.innerHTML = `<span class="err">${esc(target.error)}</span>`; return; }

  $('#who').innerHTML = `${esc(target.category)} &middot; ${esc(target.location)}`;

  const n = (target.areas || []).length;
  if (!editing) {
    el.innerHTML = `<div class="row" style="justify-content:space-between">
      <div>
        <div style="font-size:19px;font-weight:600">${esc(target.category)}</div>
        <div class="sub">${n ? n + ' search area' + (n > 1 ? 's' : '')
          : 'one area: ' + esc(target.location)} &middot; ${target.batch_size} per run</div>
      </div>
      <button onclick="drawTarget(true)">Change</button>
    </div>`;
    return;
  }

  el.innerHTML = `
    <label class="sub" for="t-cat">Type of business</label>
    <input id="t-cat" style="width:100%;margin:5px 0 14px" value="${esc(target.category)}"
           placeholder="hair salon">
    <label class="sub" for="t-loc">Main location</label>
    <input id="t-loc" style="width:100%;margin:5px 0 4px" value="${esc(target.location)}"
           placeholder="Georgetown, Penang, Malaysia">
    <p class="hint" style="margin:0 0 14px">Used in the outreach copy, and searched
      when no areas are listed below.</p>
    <label class="sub" for="t-areas">Search areas &mdash; one per line</label>
    <textarea id="t-areas" style="height:190px;margin-top:5px"
      placeholder="Georgetown, Penang, Malaysia">${esc((target.areas || []).join('\\n'))}</textarea>
    <p class="hint">Google gives about 60 results per area and no more, so one area
      runs dry in a day or two. List several and each run moves down the list,
      skipping businesses you already have.</p>
    <label class="sub" for="t-batch">Most a single run may do</label>
    <input id="t-batch" type="number" min="1" max="200" style="width:110px;margin:5px 0 4px"
           value="${target.batch_size}">
    <p class="hint">Your ceiling, for every step. The <b>How many</b> box below
      can go under this but never over it, so a slipped digit cannot turn into a
      hundred-lead bill.</p>
    <div class="row" style="margin-top:10px">
      <button class="primary" onclick="saveTarget()">Save</button>
      <button onclick="drawTarget(false)">Cancel</button>
      <span class="sub" id="t-msg"></span>
    </div>
    <p class="hint">Changing this starts a fresh hunt. Nothing already on your
      Sheet is altered or lost.</p>`;
}

async function saveTarget() {
  const body = {
    category: $('#t-cat').value,
    location: $('#t-loc').value,
    areas: $('#t-areas').value.split('\\n').map(s => s.trim()).filter(Boolean),
    batch_size: $('#t-batch').value.trim(),
  };
  document.querySelectorAll('#target button').forEach(b => b.disabled = true);
  $('#t-msg').textContent = 'Saving…';

  let d;
  try {
    d = await (await fetch('/api/campaign', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    })).json();
  } catch { d = {error: 'Could not reach the dashboard'}; }

  if (!d.ok) {
    $('#t-msg').innerHTML = `<span class="err">${esc(d.error || 'Failed')}</span>`;
    document.querySelectorAll('#target button').forEach(b => b.disabled = false);
    return;
  }
  target = Object.assign({}, target, d);
  drawTarget(false);
}

// ------------------------------------------------------------ step 2: review

let review = [];
let at = 0;

function esc(s) {
  return (s || '').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
}

async function loadReview(keepPosition) {
  let d;
  try { d = await (await fetch('/api/review-queue')).json(); } catch { return; }
  const el = $('#review');
  if (d.error) { el.innerHTML = `<div class="card err">${esc(d.error)}</div>`; return; }
  review = d.queue || [];
  if (!keepPosition) at = 0;
  if (at >= review.length) at = Math.max(0, review.length - 1);
  drawReview();
}

function drawReview() {
  const el = $('#review');
  if (!review.length) {
    el.innerHTML = '<div class="card"><div class="what">Nothing to review.</div>' +
      '<div class="sub">Briefs appear here after step 1 writes them.</div></div>';
    return;
  }
  const q = review[at];
  const facts = [];
  if (q.category) facts.push(esc(q.category));
  if (q.rating) facts.push(`${esc(q.rating)}&#9733; (${esc(q.reviews || '0')} reviews)`);
  if (q.phone) facts.push(esc(q.phone));
  if (q.social) facts.push(esc(q.social));
  if (q.maps_url) facts.push(
    `<a href="${esc(q.maps_url)}" target="_blank" rel="noopener">See it on Maps</a>`);

  const notes = [];
  if (q.angle) notes.push(`<p class="note"><b>Angle:</b> ${esc(q.angle)}</p>`);
  if (q.pain) notes.push(`<p class="note"><b>Their problem:</b> ${esc(q.pain)}</p>`);
  if (q.praise) notes.push(`<p class="note"><b>Customers say:</b> ${esc(q.praise)}</p>`);

  el.innerHTML = `<div class="card q">
    <div class="row" style="justify-content:space-between">
      <h3 style="margin:0">${esc(q.name)}</h3>
      <span class="counter">${at + 1} of ${review.length}</span>
    </div>
    <div class="facts">${facts.join('<span>&middot;</span>')}</div>
    ${notes.join('')}
    <details>
      <summary class="sub">Read the full brief before approving</summary>
      <textarea id="rp" readonly style="margin-top:9px">${esc(q.prompt)}</textarea>
    </details>
    <div class="row" style="margin-top:12px">
      <button class="approve" onclick="decide('approved')">Approve &mdash; build this</button>
      <button onclick="decide('skip')">Skip</button>
      <button onclick="move(1)" ${review.length < 2 ? 'disabled' : ''}>Decide later &rsaquo;</button>
      <span class="sub" id="rmsg"></span>
    </div>
    <div class="row" style="margin-top:10px">
      <button onclick="regen('')">Rewrite this brief</button>
      <button onclick="regenAsk()">Rewrite with my notes&hellip;</button>
    </div>
    <p class="hint">Not happy with it? <b>Rewrite this brief</b> asks again from
      scratch &mdash; the model is not deterministic, so you get a different
      take, not the same one back. <b>Rewrite with my notes</b> lets you say what
      to change: &ldquo;more upmarket&rdquo;, &ldquo;focus on the wedding
      packages&rdquo;, &ldquo;less formal, it is a family shop&rdquo;. One model
      call either way, and your notes are used once, not remembered.</p>
    <p class="hint">Approving queues it for step 3. Nothing is spent until you
      build it there.</p>
  </div>`;
}

async function regenAsk() {
  const notes = prompt('What should change about this brief?\n\ne.g. more upmarket, focus on the wedding packages, less formal');
  if (notes === null) return;          // cancelled -- do nothing, spend nothing
  if (!notes.trim()) return;
  regen(notes.trim());
}

async function regen(notes) {
  const q = review[ri];
  if (!q) return;
  $('#rmsg').textContent = notes ? 'Rewriting with your notes\u2026' : 'Rewriting\u2026';
  let d;
  try {
    d = await (await fetch('/api/regenerate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: q.lead_id, notes: notes || ''})
    })).json();
  } catch { d = {error: 'Could not reach the dashboard'}; }

  if (!d.ok) {
    $('#rmsg').innerHTML = `<span class="err">${esc(d.error || 'Failed')}</span>`;
    return;
  }
  // Show it straight away. Sending him to the Sheet to find out whether the
  // rewrite helped is how a ten-second decision becomes a two-minute one.
  q.prompt = d.prompt;
  drawReview();
  $('#rmsg').textContent = 'Rewritten. Read it before approving.';
}

function move(n) {
  if (!review.length) return;
  at = (at + n + review.length) % review.length;
  drawReview();
}

async function decide(decision) {
  const q = review[at];
  if (!q) return;
  document.querySelectorAll('#review button').forEach(b => b.disabled = true);
  $('#rmsg').textContent = 'Saving…';

  let d;
  try {
    d = await (await fetch('/api/decide', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: q.lead_id, decision})
    })).json();
  } catch { d = {error: 'Could not reach the dashboard'}; }

  if (!d.ok) {
    $('#rmsg').innerHTML = `<span class="err">${esc(d.error || 'Failed')}</span>`;
    document.querySelectorAll('#review button').forEach(b => b.disabled = false);
    return;
  }

  // Drop it locally and show the next one straight away -- waiting on a Sheets
  // round trip between every decision is what makes reviewing sixty a chore.
  review.splice(at, 1);
  if (at >= review.length) at = Math.max(0, review.length - 1);
  drawReview();
  refresh();
  loadQueue();
}

// ------------------------------------------------------------- step 3: build

async function loadQueue() {
  let d;
  try { d = await (await fetch('/api/build-queue')).json(); } catch { return; }
  const el = $('#queue');
  if (d.error) { el.innerHTML = `<div class="card err">${d.error}</div>`; return; }
  if (!d.queue || !d.queue.length) {
    el.innerHTML = '<div class="card"><div class="what">Nothing approved yet.</div>' +
      '<div class="sub">Approve a brief in step 2 and it appears here.</div></div>';
    return;
  }
  el.innerHTML = d.queue.map((q, i) => `
    <div class="card q">
      <h3>${q.name}</h3>
      <textarea id="p${i}" readonly>${q.prompt.replace(/</g,'&lt;')}</textarea>
      <div class="row" style="margin-top:10px">
        <button onclick="copyPrompt(${i})">Copy prompt</button>
        <a href="https://lovable.dev" target="_blank" rel="noopener">
          <button>Open Lovable</button></a>
        <span class="sub" id="c${i}"></span>
      </div>
      <div class="row" style="margin-top:10px">
        <input id="u${i}" placeholder="Paste the finished site URL here">
        <button onclick="saveBuilt('${q.lead_id}', ${i})">Mark as built</button>
        <span class="ok" id="s${i}"></span>
      </div>
    </div>`).join('');
}

async function copyPrompt(i) {
  const t = document.getElementById('p' + i);
  t.select();
  try { await navigator.clipboard.writeText(t.value); }
  catch { document.execCommand('copy'); }
  document.getElementById('c' + i).textContent = 'Copied. Paste it into Lovable.';
}

async function saveBuilt(leadId, i) {
  const url = document.getElementById('u' + i).value.trim();
  const r = await fetch('/api/record-build', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({lead_id: leadId, preview_url: url})
  });
  const d = await r.json();
  document.getElementById('s' + i).textContent = d.ok ? 'Saved.' : (d.error || 'Failed');
  if (d.ok) { loadQueue(); refresh(); }
}

refresh();
loadTarget();
loadModel();
loadNextUp();
loadReview();
loadQueue();
poll();
setInterval(poll, 1200);
setInterval(refresh, 30000);
setInterval(loadQueue, 30000);
// Keeps the position you are at, so a background refresh never yanks the card
// out from under a decision you are part-way through making.
setInterval(() => loadReview(true), 30000);
</script>
</body>
</html>
"""
