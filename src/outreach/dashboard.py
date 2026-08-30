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
import time
import webbrowser
from collections import deque
import os
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from . import schema
from .config import Config, load_config
from .stages.reachability import stored_verdict
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
    "discovery-refresh": ["run", "discovery", "--refresh"],
    "research": ["run", "research"],
    "reachability": ["run", "reachability"],
    "reachability-recheck": ["run", "reachability", "--recheck"],
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
    "discovery-refresh",
    "research",
    "reachability",
    "reachability-recheck",
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

# How many names the Next-up preview will render. The point is to curate the
# run before it happens, so it follows the How-many box -- but sixty names is
# a wall, not a list, so it stops here and says so.
PREVIEW_CAP = 25


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


def _is_drafted_outreach(row: dict[str, str]) -> bool:
    """Whether an Outreach Log row represents a drafted message.

    ``Status`` is a manual column the draft stage seeds to "Drafted" on
    insert, so the normal test is ``Status == "drafted"``. A row whose Status
    cell was cleared by hand still carries a message and a ``Drafted At``
    timestamp, so accept it too -- otherwise the send queue and the summary
    silently drop work the pipeline already did.
    """
    status = (row.get("Status") or "").strip().casefold()
    if status:
        return status == "drafted"
    # Empty Status: only accept rows that carry the draft's own markers, so a
    # blank row is not mistaken for a draft.
    return bool(
        (row.get("WhatsApp Message") or "").strip()
        and (row.get("Drafted At") or "").strip()
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
    from .providers.whatsapp import WA_SENT_STATUSES
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
        # That includes the WhatsApp gate: research skips a lead WhatsApp has
        # refused, so counting it here would promise work that will not
        # happen and make the run look like it under-delivered.
        if (
            lead_id not in research_ids
            and rank(current) < rank(Stage.RESEARCHED)
            and stored_verdict(row) != "no"
        ):
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
        if not _is_drafted_outreach(row):
            continue
        channel = (row.get("Channel") or "").strip().casefold()
        if channel == "email":
            summary.emails_waiting += 1
        elif channel == "unreachable":
            # No email and a landline phone -- nothing to send, so it is not
            # waiting on WhatsApp either.
            continue
        else:
            # A drafted WhatsApp lead whose WhatsApp Status says it has already
            # gone out is not waiting on you -- the send queue hides it for the
            # same reason (see /api/whatsapp/queue). Counting it here would put
            # "WhatsApp to send" upstairs while section 4 stays empty.
            wa_status = (row.get("WhatsApp Status") or "").strip().casefold()
            if wa_status in WA_SENT_STATUSES:
                continue
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

    ``limit`` must match the number the run will actually take. Previewing six
    of a fifteen-lead run means nine are chosen with nobody looking, which is
    the opposite of what this is for.

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
                # The ID is what makes a row actionable rather than decorative.
                "lead_id": row.get("Lead ID", ""),
                "name": row.get("Business Name", "") or row.get("Lead ID", ""),
                "social": row.get("Website Status") == "social_only",
                "rating": row.get("Rating", ""),
                "reviews": row.get("Review Count", ""),
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
            # The stage wrote to the Sheet from a subprocess, so nothing in
            # this process saw it happen. Drop the cached reads before the
            # page asks again -- otherwise the messages just drafted would be
            # served from a snapshot taken before they existed, which is the
            # "I drafted them and section 4 didn't change" bug wearing a
            # different hat.
            _invalidate_sheets_cache()
            with self._lock:
                self.running = None


class _SharedSheets:
    """A short-lived read cache in front of one SheetsClient.

    Four panels refresh on the same 30-second tick, and between them they read
    the same five tabs thirteen times. Sheets allows sixty reads a minute per
    user, so an idle dashboard spent close to half the budget and a drafting
    run -- which reads the Sheet itself, from a subprocess, on the same quota
    -- pushed it over. The panel then blanked with a 429 that had nothing to
    do with the data in it.

    Correctness comes from invalidating, not from the TTL being short: every
    dashboard write clears the cache, and so does the end of a run. So the
    window is only ever the few seconds between panels that are showing the
    same snapshot anyway, and nothing the user just did is served from before
    they did it.
    """

    def __init__(self, inner, ttl: float = 8.0) -> None:
        self._inner = inner
        self._ttl = ttl
        self._entries: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._fetching: dict[str, threading.Lock] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._entries.clear()

    def _entry(self, title: str):
        with self._lock:
            return self._entries.get(title)

    def _fetch_lock(self, title: str) -> threading.Lock:
        with self._lock:
            lock = self._fetching.get(title)
            if lock is None:
                lock = self._fetching[title] = threading.Lock()
            return lock

    def read_rows(self, tab):
        entry = self._entry(tab.title)
        if entry is not None and time.monotonic() - entry[0] < self._ttl:
            return entry[1]

        # One fetch per tab, not one per caller. The panels refresh together,
        # so on a cold cache every one of them misses at the same instant --
        # without this they would each issue the same request and the cache
        # would save nothing at exactly the moment the quota is tightest.
        # Readers of other tabs are unaffected; only same-tab callers wait,
        # and what they wait for is the answer they were about to ask for.
        with self._fetch_lock(tab.title):
            entry = self._entry(tab.title)
            if entry is not None and time.monotonic() - entry[0] < self._ttl:
                return entry[1]

            try:
                rows = self._inner.read_rows(tab)
            except Exception:
                # 'Read requests per minute' clears by waiting, so blanking
                # the page over it is worse than showing the last good answer
                # for one more tick. Only a read we have never done
                # successfully is fatal.
                if entry is not None:
                    log.warning(
                        "Reading %s failed; serving the previous rows.",
                        tab.title, exc_info=True,
                    )
                    return entry[1]
                raise

            with self._lock:
                self._entries[tab.title] = (time.monotonic(), rows)
            return rows

    # Writes go through untouched, but every one of them invalidates: a cached
    # read taken before an approval is exactly the stale answer that would make
    # the button look broken.
    def upsert(self, *args, **kwargs):
        try:
            return self._inner.upsert(*args, **kwargs)
        finally:
            self.invalidate()

    def update_column(self, *args, **kwargs):
        try:
            return self._inner.update_column(*args, **kwargs)
        finally:
            self.invalidate()

    def append(self, *args, **kwargs):
        try:
            return self._inner.append(*args, **kwargs)
        finally:
            self.invalidate()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _PerThreadClient:
    """One SheetsClient per thread, because httplib2 is not thread-safe.

    The Flask dev server handles each request on its own thread, and the panels
    refresh together -- so a single shared client means several threads writing
    to one TLS socket at once. The frames interleave and the read comes back as
    ``SSLError: WRONG_VERSION_NUMBER``, which looks like a network fault and is
    really a concurrency bug.

    Only the HTTP transport has to be per-thread. The rows it returns are plain
    dicts, so the cache in front of this can still be shared -- which is the
    whole point, since that is what keeps the read quota down.
    """

    def __init__(self, key: tuple) -> None:
        self._key = key
        self._local = threading.local()
        # Building credentials can refresh and rewrite the token file, so two
        # threads arriving at once must not both do it.
        self._build_lock = threading.Lock()

    def _client(self):
        existing = getattr(self._local, "client", None)
        if existing is not None:
            return existing
        with self._build_lock:
            from .auth import get_credentials
            from .sheets import SheetsClient

            # allow_consent=False: nobody is watching a terminal here. Consent
            # opens a browser and blocks until it is completed, so an expired
            # token would hang this request thread rather than fail it, and
            # the page would just stop answering.
            creds = get_credentials(
                self._key[0], self._key[1], allow_consent=False
            )
            client = SheetsClient(creds, self._key[2])
        self._local.client = client
        return client

    def __getattr__(self, name):
        return getattr(self._client(), name)


_shared_sheets: _SharedSheets | None = None
_shared_sheets_key: tuple | None = None
_shared_sheets_lock = threading.Lock()


def _sheets_client(config: Config):
    """The process-wide cached view of the Sheet.

    The cache is shared so it actually spans the panels it exists to collapse;
    the transport underneath it is per-thread. Rebuilt if the campaign is
    switched to a different Sheet or token.
    """
    global _shared_sheets, _shared_sheets_key
    key = (
        config.secrets.oauth_client_file,
        config.secrets.token_file,
        config.secrets.sheet_id,
    )
    with _shared_sheets_lock:
        if _shared_sheets is None or _shared_sheets_key != key:
            _shared_sheets = _SharedSheets(_PerThreadClient(key))
            _shared_sheets_key = key
        return _shared_sheets


def _invalidate_sheets_cache() -> None:
    """Drop every cached read. Called when a run ends and on config changes."""
    with _shared_sheets_lock:
        if _shared_sheets is not None:
            _shared_sheets.invalidate()


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
        return render_template_string(
            PAGE,
            campaign=current().campaign,
            build=current().build,
            whatsapp_enabled=current().outreach.whatsapp_enabled,
        )

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
                "sender_name": c.outreach.sender_name,
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
                "outreach_model": c.outreach.model,
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
        from .campaign_edit import CampaignEditError, rewrite_outreach, rewrite_research  # noqa: F401
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
        # Blank is allowed here: it means "use the research model", so clearing
        # the field is a legitimate way to go back to one model for everything.
        outreach_model = payload.get("outreach_model")
        if outreach_model is not None:
            outreach_model = outreach_model.strip()
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
            updated = rewrite_outreach(updated, model=outreach_model)
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

        sender_name = payload.get("sender_name")
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
                sender_name=sender_name,
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
                "sender_name": c.outreach.sender_name,
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
        # Follow the How-many box: if fifteen will run, show fifteen. Capped
        # so that setting sixty produces a list you can still read.
        raw = request.args.get("limit")
        try:
            want = int(raw) if raw else 6
        except ValueError:
            want = 6
        shown = max(1, min(want, PREVIEW_CAP))
        try:
            sheets = _sheets_client(c)
            preview = queue_preview(
                sheets.read_rows(schema.LEADS),
                set(sheets.read_rows(schema.RESEARCH)),
                set(sheets.read_rows(schema.SITE_BRIEFS)),
                social_first=c.research.social_first,
                limit=shown,
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({"queues": preview, "shown": shown, "cap": PREVIEW_CAP})

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

    @app.route("/api/outreach/rewrite", methods=["POST"])
    def api_outreach_rewrite():
        """Rewrite one lead's outreach message. One model call.

        The WhatsApp panel's Rewrite button. Re-rolls the copy for a lead whose
        draft is already written -- generic, or missing the thing that sells
        the business. Returns the new message so the panel can show it at once.
        """
        from .providers.llm import make_writer
        from .stages.outreach_draft import regenerate_one

        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()

        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400

        try:
            config = current()
            sheets = _sheets_client(config)
            writer = make_writer(config, model=config.outreach.model or None)
            copy = regenerate_one(config, sheets, writer, lead_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({"ok": True, **copy})

    @app.route("/api/drop", methods=["POST"])
    def api_drop():
        """Take one lead out of every queue, for good.

        Marks it Disqualified, which every stage already skips -- so this
        needs no new machinery and cannot half-apply. It is deliberately the
        only per-lead control on the preview: you act on the few you do not
        want, rather than ticking the many you do, and a lead you have judged
        once never comes back to be judged again.
        """
        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400
        try:
            sheets = _sheets_client(current())
            sheets.update_column(
                schema.LEADS, "Stage", {lead_id: Stage.DISQUALIFIED.value}
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({"ok": True})

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

    # -------------------------------------------------------------- WhatsApp

    @app.route("/api/whatsapp/status")
    def api_whatsapp_status():
        """Relay connection state + daily counters."""
        c = current()
        if not c.outreach.whatsapp_enabled:
            return jsonify({"enabled": False})
        try:
            from .providers.whatsapp import WhatsAppClient

            client = WhatsAppClient(port=c.outreach.whatsapp_relay_port)
            return jsonify({"enabled": True, **client.get_status()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"enabled": True, "connected": False, "error": str(exc)})

    @app.route("/api/whatsapp/qr")
    def api_whatsapp_qr():
        """Base64 PNG of the pairing QR, or null if already paired."""
        c = current()
        if not c.outreach.whatsapp_enabled:
            return jsonify({"qr": None})
        try:
            from .providers.whatsapp import WhatsAppClient

            client = WhatsAppClient(port=c.outreach.whatsapp_relay_port)
            return jsonify({"qr": client.get_qr()})
        except Exception:
            return jsonify({"qr": None})

    @app.route("/api/suggest-areas", methods=["POST"])
    def api_suggest_areas():
        """Expand a broad location into the sub-areas discovery can drink.

        One model call. Nothing is saved -- the list goes back into the
        textarea for Lyon to edit and cut before he presses Save, because a
        suggested area is a guess and some of them will be wrong.
        """
        from .providers.llm import build_areas_prompt, make_writer

        payload = request.get_json(silent=True) or {}
        location = str(payload.get("location") or "").strip()
        if not location:
            return jsonify({"error": "Type a location first."}), 400
        try:
            count = max(5, min(40, int(payload.get("count") or 20)))
        except (TypeError, ValueError):
            count = 20

        c = current()
        try:
            areas = make_writer(c).suggest_areas(
                build_areas_prompt(location, count)
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced in the panel
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502

        return jsonify({"areas": list(areas), "location": location})

    @app.route("/api/reachability")
    def api_reachability():
        """Who the relay has confirmed on WhatsApp, and who is still waiting.

        Reads the cached verdicts written by the check at the end of
        research. Purely a view -- it never asks WhatsApp anything, so
        opening the dashboard costs no lookups.
        """
        from . import phone as phone_lib
        from .stages.reachability import CHECKABLE, _country_of, has_email, stored_verdict

        c = current()
        if not c.secrets.sheet_id:
            return jsonify({"verified": [], "rejected": [], "waiting": [], "counts": {}})
        try:
            sheets = _sheets_client(c)
            leads = sheets.read_rows(schema.LEADS)
            research = sheets.read_rows(schema.RESEARCH)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502

        verified, rejected, waiting = [], [], []
        for lead_id, (_row, lead) in leads.items():
            # Discovered leads belong here now: the check can run before
            # research, so this panel is what tells Lyon which leads are
            # worth researching at all.
            if (lead.get("Stage") or "").strip() not in CHECKABLE:
                continue
            found = research.get(lead_id)
            row = found[1] if found else {}
            verdict = stored_verdict(lead, row)
            country = _country_of(lead, c.compliance.country_code)
            entry = {
                "lead_id": lead_id,
                "name": lead.get("Business Name", "") or lead_id,
                "phone": lead.get("Phone", ""),
                "dialled": phone_lib.normalise(lead.get("Phone", ""), country),
                "country": country,
                "email": has_email(row),
                "checked_at": row.get("WhatsApp Checked At", ""),
            }
            if verdict == "yes":
                verified.append(entry)
            elif verdict == "no":
                rejected.append(entry)
            else:
                waiting.append(entry)

        return jsonify(
            {
                "verified": verified,
                "rejected": rejected,
                "waiting": waiting,
                "counts": {
                    "verified": len(verified),
                    "rejected": len(rejected),
                    "waiting": len(waiting),
                    # A rejected lead still gets built when it has an email,
                    # so "will be built" is not the same as "on WhatsApp".
                    "buildable": len(verified) + sum(1 for e in rejected if e["email"]),
                },
            }
        )

    def _shot_for(cfg, lead, lead_id):
        """The lead's screenshot on disk, or None.

        Both homes: beside a page this pipeline generated, and in the lead's
        slug directory when the site exists only at a URL.
        """
        from . import screenshot as shot_mod
        from .stages.outreach_draft import shot_dir_for, site_page_for

        candidates = []
        page = site_page_for(cfg, lead, lead_id)
        if page is not None:
            candidates.append(shot_mod.shot_path_for(page))
        candidates.append(shot_dir_for(cfg, lead, lead_id) / "screenshot.png")
        for path in candidates:
            if path.exists() and path.stat().st_size:
                return path
        return None

    @app.route("/api/screenshot/<lead_id>")
    def api_screenshot(lead_id: str):
        """Serve one lead's screenshot so it can be checked before sending.

        Resolved from the lead's own slug, never from a path supplied by the
        page -- a filename off the wire would let any file on disk be read.
        """
        from flask import send_file

        c = current()
        try:
            leads = _sheets_client(c).read_rows(schema.LEADS)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502

        entry = leads.get(lead_id)
        if entry is None:
            return jsonify({"error": "No such lead"}), 404

        path = _shot_for(c, entry[1], lead_id)
        if path is None:
            return jsonify({"error": "No screenshot for this lead yet"}), 404
        return send_file(path.resolve(), mimetype="image/png")

    @app.route("/api/whatsapp/queue")
    def api_whatsapp_queue():
        """Leads waiting for WhatsApp send, with phone + message text."""
        from . import phone as phone_lib
        from .providers.whatsapp import WA_SENT_STATUSES
        from .stages.reachability import _country_of, can_whatsapp

        c = current()
        if not c.secrets.sheet_id:
            return jsonify({"queue": [], "excluded": []})
        try:
            sheets = _sheets_client(c)
            outreach = sheets.read_rows(schema.OUTREACH_LOG)
            leads = sheets.read_rows(schema.LEADS)
            # For the cached WhatsApp verdict. Worth the extra read: without
            # it a US number falls back to the numbering-plan guess, which
            # cannot tell mobile from landline there and hides the lead.
            research = sheets.read_rows(schema.RESEARCH)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200

        queue: list[dict[str, str]] = []
        # Every message the pipeline drafted has to end up in one of these two
        # lists. A drafted lead that appears in neither is a silent failure --
        # section 4 goes quiet and the only way to find out why is to open the
        # Sheet and read columns by eye. So the loop below never just skips a
        # drafted row: it either queues it or records why not.
        excluded: list[dict[str, str]] = []

        for lead_id, (_n, row) in outreach.items():
            name = row.get("Business Name") or ""
            channel = (row.get("Channel") or "").strip().casefold()
            message = (row.get("WhatsApp Message") or "").strip()
            drafted_at = (row.get("Drafted At") or "").strip()
            lead = leads.get(lead_id, (0, {}))[1]
            phone = lead.get("Phone", "").strip()

            def drop(reason: str, **extra: str) -> None:
                entry = {
                    "lead_id": lead_id,
                    "name": name,
                    "phone": phone,
                    "reason": reason,
                }
                entry.update(extra)
                excluded.append(entry)

            # A row with neither a message nor a timestamp is not drafted work
            # -- an empty row, or one of another channel's records. Passing
            # over it is not a silent drop, because there is nothing to show.
            if not message and not drafted_at:
                continue

            # Carries a draft but the Status cell says something else. That is
            # either a hand-edit or a row written under an older column order,
            # and it is exactly the case that used to vanish -- so surface it
            # with the offending value rather than dropping it.
            if not _is_drafted_outreach(row):
                drop("status_not_drafted", status=(row.get("Status") or "").strip())
                continue

            # A landline-only lead (no email, no mobile) is drafted as
            # "Unreachable". It can't receive WhatsApp, but it must still show
            # at the bottom of section 4 with the reason -- hiding it made a
            # founder think the draft never saved.
            if channel == "unreachable":
                drop("unreachable")
                continue

            # Drafted with an email address, so it went to Gmail drafts rather
            # than here. Worth listing: the WhatsApp text exists on the row and
            # its absence from the send list is otherwise unexplained.
            if channel == "email":
                drop("email_channel", recipient=(row.get("Recipient") or "").strip())
                continue

            if channel != "whatsapp":
                drop("unknown_channel", channel=(row.get("Channel") or "").strip())
                continue

            # Skip if already sent via the dashboard.
            wa_status = (row.get("WhatsApp Status") or "").strip().casefold()
            if wa_status in WA_SENT_STATUSES:
                drop("already_sent", wa_status=wa_status)
                continue

            if not phone:
                # The lead row exists in Outreach Log but has no matching row in
                # Leads, or the Phone cell is blank. Either way the message has
                # nowhere to go -- say so instead of vanishing silently.
                drop("no_lead_row" if lead_id not in leads else "phone_empty")
                continue

            # Hide anything that cannot receive WhatsApp, so the queue only
            # shows leads we can actually reach. The cached verdict from the
            # reachability check decides where there is one; the numbering
            # plan is only a fallback for leads it has not reached yet.
            research_row = research.get(lead_id)
            if not can_whatsapp(
                lead,
                research_row[1] if research_row else None,
                c.compliance.country_code,
            ):
                drop("phone_not_mobile")
                continue

            queue.append(
                {
                    "lead_id": lead_id,
                    "name": name or lead.get("Business Name", ""),
                    "phone": phone,
                    # Dialled form, resolved here where the lead's country is
                    # in hand. The browser never works this out and the relay
                    # must not guess it, so it is computed once, server-side,
                    # and carried through the send.
                    "phone_e164": phone_lib.normalise(
                        phone, _country_of(lead, c.compliance.country_code)
                    ),
                    "message": row.get("WhatsApp Message", ""),
                    # Whether a picture of the site will be attached. Shown
                    # in the queue because until now the only way to find
                    # out was to send the message and look at your phone.
                    "has_shot": _shot_for(c, lead, lead_id) is not None,
                }
            )

        return jsonify({"queue": queue, "excluded": excluded})

    @app.route("/api/whatsapp/send", methods=["POST"])
    def api_whatsapp_send():
        """Send one WhatsApp message and record the result in the Sheet."""
        c = current()
        if not c.outreach.whatsapp_enabled:
            return jsonify({"error": "WhatsApp sending is not enabled"}), 400

        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        # The queue computed this from the lead's country. Preferred over the
        # display number, which carries no country and cannot be dialled.
        #
        # When it is present the number is ALREADY E.164, so no country is
        # passed on: normalising a second time with the campaign's country
        # glued that country's code onto a number that already had its own.
        # A UK lead went out as 60447450225230 -- a JID Baileys accepts and
        # reports as sent, delivered to nobody. Only the raw display number,
        # which carries no country, may be normalised here.
        dialled = (payload.get("phone_e164") or "").strip()
        phone = dialled or (payload.get("phone") or "").strip()
        send_country = "" if dialled else c.compliance.country_code
        message = (payload.get("message") or "").strip()

        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400
        if not phone:
            return jsonify({"error": "phone is required"}), 400
        if not message:
            return jsonify({"error": "message is required"}), 400

        from . import screenshot
        from .providers.whatsapp import WhatsAppClient, WhatsAppError
        from .stages.outreach_draft import shot_dir_for, site_page_for

        # One read, only to locate the lead's built page.
        try:
            leads_for_shot = {
                k: v[1] for k, v in _sheets_client(c).read_rows(schema.LEADS).items()
            }
        except Exception:  # noqa: BLE001 -- a picture is never worth a failed send
            leads_for_shot = {}

        try:
            client = WhatsAppClient(port=c.outreach.whatsapp_relay_port)
            # The screenshot taken at draft time, if the page is still
            # there. Looked up here rather than trusted from the browser:
            # a path from the page could point anywhere on disk.
            lead_row = leads_for_shot.get(lead_id)
            shot = _shot_for(c, lead_row, lead_id) if lead_row is not None else None

            result = client.send_message(phone, message, send_country, shot)
        except WhatsAppError as exc:
            # Record the failure so the user sees it in the Sheet.
            try:
                sheets = _sheets_client(c)
                sheets.update_column(
                    schema.OUTREACH_LOG,
                    "WhatsApp Status",
                    {lead_id: "failed"},
                )
            except Exception:
                pass
            return jsonify({"error": str(exc)}), 200

        # Record success in the Sheet.
        try:
            sheets = _sheets_client(c)
            sheets.update_column(
                schema.OUTREACH_LOG,
                "WhatsApp Status",
                {lead_id: result.status},
            )
            sheets.update_column(
                schema.OUTREACH_LOG,
                "WhatsApp Sent At",
                {lead_id: result.timestamp},
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify(
                {
                    "error": (
                        f"Message sent but could not update Sheet: {exc}. "
                        "The message was delivered — mark it manually."
                    )
                }
            ), 200

        return jsonify(
            {
                "ok": True,
                "message_id": result.message_id,
                "status": result.status,
                "timestamp": result.timestamp,
            }
        )

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
  button.link { background:none; border:none; padding:0 0 0 6px; font-size:12px;
    color:#8a8f98; text-decoration:underline; cursor:pointer; }
  button.link:hover { color:#c0392b; }
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
  /* ---- WhatsApp panel ---- */
  .wa-panel { margin-top:12px; }
  .wa-status { display:flex; align-items:center; gap:8px; margin-bottom:12px;
               font-size:14px; }
  .wa-dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
  .wa-dot.on { background:var(--ok); }
  .wa-dot.off { background:var(--accent); }
  .wa-dot.wait { background:var(--warn); }
  .wa-qr-wrap { text-align:center; margin:12px 0; }
  .wa-qr-wrap img { max-width:220px; border-radius:8px; border:1px solid var(--line); }
  .wa-qr-wrap .sub { margin-top:6px; }
  .wa-msg-card { border:1px solid var(--line); border-radius:8px; padding:12px;
                 margin-bottom:10px; }
  .wa-msg-card .wa-name { font-weight:600; font-size:15px; }
  .wa-msg-card .wa-phone { font-size:13px; color:var(--muted); margin-bottom:6px; }
  .wa-msg-card .wa-edit { width:100%; font:inherit; font-size:13px; line-height:1.45;
                           padding:8px 10px; border-radius:6px; margin-bottom:10px;
                           border:1px solid var(--line); background:var(--bg); color:var(--ink);
                           resize:vertical; min-height:60px; box-sizing:border-box; }
  .wa-msg-card .wa-row { display:flex; justify-content:space-between; align-items:center;
                         gap:10px; }
  .wa-msg-card .wa-state { font-size:13px; }
  .wa-shot { display:flex; align-items:center; gap:8px; margin:6px 0;
             text-decoration:none; color:var(--muted); font-size:12px; }
  .wa-shot img { width:52px; height:70px; object-fit:cover; object-position:top;
                 border:1px solid var(--line); border-radius:4px; }
  .wa-msg-card .wa-state.ok { color:var(--ok); }
  .wa-msg-card .wa-state.err { color:var(--accent); }
  button.wa-send { background:var(--ok); border-color:var(--ok); color:#fff; }
  button.wa-send:hover:not(:disabled) { opacity:.9; color:#fff; }
  button.wa-send.confirm { background:var(--accent); border-color:var(--accent); }
  .wa-actions { display:flex; gap:8px; }
  button.wa-rewrite { background:transparent; border-color:var(--line); color:var(--ink); }
  button.wa-rewrite:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
  .wa-spin { display:inline-block; width:14px; height:14px; border:2px solid transparent;
             border-top-color:currentColor; border-radius:50%;
             animation:wa-spin .6s linear infinite; vertical-align:-2px;
             margin-right:4px; }
  @keyframes wa-spin { to { transform:rotate(360deg); } }
  /* Drafted-but-hidden leads, with the reason section 4 used to swallow.
     These are the ones you cannot act on, so they are laid out five across
     and scroll inside a fixed height -- present and countable, but never
     pushing the messages you *can* send off the screen. */
  .wa-excl-wrap { margin-top:12px; border:1px dashed var(--line); border-radius:8px;
                  padding:10px 12px; background:var(--bg); }
  .wa-excl-head { font-size:13px; font-weight:600; color:var(--ink); margin-bottom:8px; }
  .wa-excl-grid { display:grid; grid-template-columns:repeat(5, minmax(0, 1fr));
                  gap:8px; max-height:230px; overflow-y:auto; padding-right:4px; }
  @media (max-width:900px) { .wa-excl-grid { grid-template-columns:repeat(2, minmax(0, 1fr)); } }
  .wa-excl { padding:7px 9px; border:1px solid var(--line); border-radius:6px;
             background:var(--card); min-width:0; }
  .wa-excl .wa-name { font-weight:600; font-size:13px; line-height:1.25;
                      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .wa-excl .wa-why { margin-top:3px; font-size:12px; color:var(--muted);
                     overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .wa-err { margin-top:8px; padding:8px 10px; border-radius:8px;
            border:1px solid var(--accent); background:var(--bg);
            color:var(--accent); font-size:13px; }
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
        <button data-run="discovery-refresh">Refresh lead details</button>
        <button data-run="research">Research</button>
        <button data-run="reachability">Check WhatsApp</button>
        <button data-run="reachability-recheck">Re-check WhatsApp</button>
        <button data-run="site-brief">Write briefs</button>
        <button data-run="site-build">Build approved</button>
        <button data-run="outreach-draft">Draft messages</button>
      </div>
      <p class="hint"><b>How many</b> above applies to these too &mdash; set it to
        5, press <b>Research</b>, and only five leads are researched.</p>
      <p class="hint"><b>Refresh lead details</b> re-reads businesses already on
        the Sheet and updates their phone number and country from Google, instead
        of hunting for new ones. Run it once over leads gathered before the phone
        number was stored in international form &mdash; without the country code a
        number cannot be dialled, and that is what made whole countries look like
        they had no WhatsApp.</p>
      <p class="hint"><b>Check WhatsApp</b> works on leads you have only just
        found, before any research is spent on them &mdash; press it straight
        after <b>Find leads</b> and section 1b tells you which ones are worth
        researching. It also runs on its own at the end of every Research, only
        asks about numbers it has no answer for, and never asks twice.</p>
      <p class="hint"><b>Re-check WhatsApp</b> asks again for leads that already
        have an answer. Use it once on any leads answered before the check
        started asking WhatsApp directly: a &ldquo;no&rdquo; worked out from the
        number&rsquo;s format alone is not trustworthy, because WhatsApp
        Business runs on landlines. It costs one lookup per lead, so it is not
        something to press daily.</p>
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

  <h2><span class="stepno">1b</span> WhatsApp check
      <span class="sub" style="text-transform:none;letter-spacing:0">
      &mdash; who can actually be messaged. No message is sent; only verified
      leads go on to a brief</span></h2>
  <div class="card" id="reach">Loading&hellip;</div>

  <h2><span class="stepno">2</span> Review
      <span class="sub" style="text-transform:none;letter-spacing:0">
      &mdash; one at a time. Approve sends it to the build queue; nothing is spent yet</span>
      <a class="sub" href="/guide#2-review-the-briefs-one-at-a-time" target="_blank" rel="noopener">What is this?</a></h2>
  <div id="review"></div>

  <h2><span class="stepno">3</span> Build
      <span class="sub" style="text-transform:none;letter-spacing:0">
      &mdash; approved, waiting for you to paste into {{ build.site_builder_name }}</span>
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
// Poured in server-side by Jinja. |tojson JSON-encodes the string and escapes
// <, >, & so a value can never break out of this <script> block -- a bare
// Jinja interpolation inside JS would HTML-escape and produce garbage, the
// exact bug CLAUDE.md warns about. These are campaign-level craft constants,
// not per-business fields, so they live here rather than on each queue card.
const SITE_BUILDER = {{ build.site_builder_name|tojson }};
const SITE_BUILDER_URL = {{ build.site_builder_url|tojson }};
const WA_ENABLED = {{ whatsapp_enabled|tojson }};

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
    'Send panel below'));
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
  // WhatsApp messages are fetched and rendered inline — no separate panel.
  if (WA_ENABLED) send.push('<div id="wa-inline"></div>');
  $('#send').innerHTML = (send.length ? send.join('') : '')
    || '<div class="what">Nothing to send yet.</div>' +
       '<div class="sub">Messages are written once a lead\\u2019s demo site is built.</div>';
  if (WA_ENABLED) loadWhatsAppInline();
}

// --------------------------------------------------------------- WhatsApp

let waState = null;  // last /api/whatsapp/status response
let waQueue = [];    // last /api/whatsapp/queue response
let waExcluded = []; // drafted WhatsApp leads dropped from the queue, with reasons

async function loadWhatsAppInline() {
  const el = $('#wa-inline');
  if (!el) return;

  // --- status
  try { waState = await (await fetch('/api/whatsapp/status')).json(); }
  catch { waState = {connected: false, error: 'Could not reach relay'}; }

  // --- queue (+ excluded: drafted leads dropped from the send list, with why)
  let waErr = null;
  try {
    const q = await (await fetch('/api/whatsapp/queue')).json();
    waQueue = q.queue || [];
    waExcluded = q.excluded || [];
    // The endpoint returns {"error": "..."} (HTTP 200) when the Sheet read
    // itself fails. Don't swallow that -- it's the difference between
    // "nothing to send" and "the dashboard can't reach your Sheet".
    if (q.error) waErr = q.error;
  } catch { waQueue = []; waExcluded = []; waErr = 'Could not reach the dashboard'; }

  let html = '';

  // A Sheet-read error blanks the queue silently. Show it up top so section 4
  // is never empty without a reason.
  if (waErr) {
    html += `<div class="wa-err"><b>Couldn\\u2019t read your Sheet:</b> ${esc(waErr)}</div>`;
  }

  // Connection indicator
  const dot = waState.connected ? 'on' : waState.paired === false ? 'wait' : 'off';
  const label = waState.connected
    ? `Connected \\u2014 ${waState.daily_sent || 0} / ${waState.daily_cap || 30} sent today`
    : waState.error || 'Relay not running';
  html += `<div class="wa-status"><span class="wa-dot ${dot}"></span>${esc(label)}</div>`;

  // QR when unpaired
  if (!waState.connected && !waState.paired) {
    try {
      const qr = await (await fetch('/api/whatsapp/qr')).json();
      if (qr.qr) {
        html += `<div class="wa-qr-wrap">
          <img src="${qr.qr}" alt="WhatsApp pairing QR">
          <div class="sub">Open WhatsApp on your phone \\u2192 Linked Devices \\u2192 Link a Device, then scan this code.</div>
        </div>`;
      }
    } catch {}
  }

  // Message cards — each has an editable textarea and a Send button
  if (waQueue.length) {
    html += waQueue.map((m, i) => {
      const msgText = esc(m.message || '');
      const sending = m._sending;
      const sent = m._sent;
      const failed = m._failed;
      let actions = '';
      if (sending) {
        actions = `<span class="wa-state"><span class="wa-spin"></span>Sending\\u2026</span>`;
      } else if (sent) {
        actions = `<span class="wa-state ok">Sent \\u2714</span>`;
      } else if (failed) {
        actions = `<span class="wa-state err">${esc(m._error || 'Failed')}</span>
          <button class="wa-send" onclick="retryWhatsApp(${i})">Retry</button>`;
      } else {
        actions = `<div class="wa-actions">
          <button class="wa-rewrite" onclick="rewriteWhatsApp(${i}, '${esc(m.lead_id)}')">Rewrite</button>
          <button class="wa-send" onclick="sendWhatsApp(${i}, '${esc(m.lead_id)}', '${esc(m.phone)}')">Send</button>
        </div>`;
      }
      // The picture that will ride with the message. Shown small, and
      // clickable, because the only way to check it before was to send it
      // and look at your phone.
      const shot = m.has_shot
        ? `<a href="/api/screenshot/${encodeURIComponent(m.lead_id)}" target="_blank"
              rel="noopener" class="wa-shot" title="Open the screenshot full size">
             <img src="/api/screenshot/${encodeURIComponent(m.lead_id)}" alt="">
             <span>Site screenshot attached</span></a>`
        : '<div class="sub" style="margin:6px 0">No screenshot &mdash; the text '
          + 'will send on its own.</div>';

      return `<div class="wa-msg-card" id="wa-msg-${i}">
        <div class="wa-name">${esc(m.name)}</div>
        <div class="wa-phone">${esc(m.phone)}</div>
        ${shot}
        <textarea class="wa-edit" id="wa-text-${i}" rows="4">${msgText}</textarea>
        <div class="wa-row">${actions}</div>
      </div>`;
    }).join('');
  } else if (waState.connected) {
    html += '<div class="sub">No messages waiting to send.</div>';
  }

  // Drafted WhatsApp leads that were dropped from the send list — show why
  // instead of leaving section 4 blank. This is the "my drafted message isn't
  // showing" answer, surfaced in the panel itself.
  if (waExcluded.length) {
    // Two strings per reason: a short tag that fits a narrow grid cell, and
    // the full sentence, which goes in the title attribute so hovering still
    // explains it without the panel growing to fit the prose.
    const reasons = {
      already_sent: (x) => [
        'Sent \\u2714',
        `Already sent \\u2014 WhatsApp Status is \\u2018${x.wa_status || 'sent'}\\u2019, so this one is done. If it didn\\u2019t actually deliver, clear the WhatsApp Status cell and it comes back.`],
      phone_empty: () => [
        'No phone',
        'No phone number in the Leads tab. Add a Malaysian mobile (01\\u2026) and it will appear here.'],
      no_lead_row: () => [
        'Not in Leads',
        'No matching row in the Leads tab \\u2014 the Outreach Log has this lead but the Leads tab doesn\\u2019t.'],
      phone_not_mobile: (x) => [
        `Landline ${x.phone || ''}`,
        `Phone \\u2018${x.phone || ''}\\u2019 isn\\u2019t a Malaysian mobile (01\\u2026). Landlines (03/04/05\\u2026) and foreign numbers can\\u2019t receive WhatsApp.`],
      unreachable: (x) => [
        `Landline ${x.phone || ''}`,
        `No email and the phone \\u2018${x.phone || ''}\\u2019 is a landline \\u2014 it can\\u2019t receive WhatsApp.`],
      email_channel: (x) => [
        `Email \\u2014 ${x.recipient || 'drafted'}`,
        `Drafted as an email to ${x.recipient || 'this lead'}, so it\\u2019s waiting in Gmail drafts rather than here.`],
      unknown_channel: (x) => [
        `Channel: ${x.channel || 'blank'}`,
        `The Channel cell says \\u2018${x.channel || ''}\\u2019, which isn\\u2019t WhatsApp, Email or Unreachable. Fix it in the Sheet and this lead comes back.`],
      status_not_drafted: (x) => [
        `Status: ${x.status || 'blank'}`,
        `This lead has a drafted message, but its Status cell says \\u2018${x.status || ''}\\u2019 rather than \\u2018Drafted\\u2019. Set it back to Drafted and it returns to the send list.`],
    };
    const items = waExcluded.map((x) => {
      const pair = (reasons[x.reason] || (() => ['Hidden', 'Hidden from the send list.']))(x);
      return `<div class="wa-excl" title="${esc(x.name || x.lead_id)} \\u2014 ${esc(pair[1])}">
        <div class="wa-name">${esc(x.name || x.lead_id)}</div>
        <div class="wa-why">${esc(pair[0])}</div>
      </div>`;
    }).join('');
    html += `<div class="wa-excl-wrap">
      <div class="wa-excl-head">${waExcluded.length} drafted message${waExcluded.length > 1 ? 's' : ''} you can\\u2019t send from here \\u2014 hover any for the reason</div>
      <div class="wa-excl-grid">${items}</div>
    </div>`;
  }

  el.innerHTML = html;
}

function sendWhatsApp(idx, leadId, phone) {
  const card = $(`#wa-msg-${idx}`);
  if (!card) return;
  const msg = waQueue[idx];
  if (!msg) return;

  // Read the current text from the textarea (user may have edited it)
  const textarea = $(`#wa-text-${idx}`);
  const message = textarea ? textarea.value : msg.message;

  // First click: confirm
  if (!msg._confirming) {
    msg._confirming = true;
    const row = card.querySelector('.wa-row');
    row.innerHTML = `<span class="wa-state">Send to ${esc(phone)}?</span>
      <button class="wa-send confirm" onclick="sendWhatsApp(${idx}, '${esc(leadId)}', '${esc(phone)}')">Yes, send</button>
      <button onclick="cancelSend(${idx})">Cancel</button>`;
    return;
  }

  // Second click: send
  msg._confirming = false;
  msg._sending = true;
  loadWhatsAppInline();

  fetch('/api/whatsapp/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    // phone is the display form; phone_e164 is what actually gets dialled.
    body: JSON.stringify({lead_id: leadId, phone: phone,
                          phone_e164: msg.phone_e164 || '', message: message}),
  }).then(r => r.json()).then(d => {
    msg._sending = false;
    if (d.error) { msg._failed = true; msg._error = d.error; }
    else { msg._sent = true; }
    loadWhatsAppInline();
    refresh();  // update counts
  }).catch(e => {
    msg._sending = false;
    msg._failed = true;
    msg._error = e.message || 'Network error';
    loadWhatsAppInline();
  });
}

function cancelSend(idx) {
  waQueue[idx]._confirming = false;
  loadWhatsAppInline();
}

function retryWhatsApp(idx) {
  waQueue[idx]._failed = false;
  waQueue[idx]._error = null;
  loadWhatsAppInline();
}

function rewriteWhatsApp(idx, leadId) {
  const card = $(`#wa-msg-${idx}`);
  if (!card) return;
  const btn = card.querySelector('.wa-rewrite');
  if (btn) { btn.disabled = true; btn.textContent = 'Rewriting\\u2026'; }

  fetch('/api/outreach/rewrite', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({lead_id: leadId}),
  }).then(r => r.json()).then(d => {
    if (d.error) {
      const row = card.querySelector('.wa-row');
      if (row) row.innerHTML = `<span class="wa-state err">${esc(d.error)}</span>`;
      return;
    }
    loadWhatsAppInline();
  }).catch(e => {
    const row = card.querySelector('.wa-row');
    if (row) row.innerHTML = `<span class="wa-state err">${esc(e.message || 'Network error')}</span>`;
  });
}

// Poll WhatsApp state every 30 seconds
if (WA_ENABLED) {
  loadWhatsAppInline();
  setInterval(loadWhatsAppInline, 30000);
}

async function poll() {
  let d;
  try { d = await (await fetch('/api/log')).json(); } catch { return; }
  if (d.lines.length) $('#log').textContent = d.lines.join('\\n');
  $('#log').scrollTop = $('#log').scrollHeight;
  const busy = !!d.running;
  $('#running').textContent = busy ? `Running ${d.running}\\u2026` : '';
  document.querySelectorAll('button[data-run]').forEach(b => b.disabled = busy);
  if (!busy && poll.wasBusy) {
    loadModel();
    loadNextUp();
    loadQueue();
  loadReach();
    // Awaited, not fired and forgotten: refresh() rebuilds the #send panel
    // and only then fills in the WhatsApp cards. Drafting finishes in step 1
    // and the messages have to be on screen in step 4 without a manual
    // reload, so the panel is rebuilt before anything else can race it.
    await refresh();
  }
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
        <div class="sub">research &amp; site brief &middot; via ${esc(m.provider)} &middot; ${keyState}</div>
        <div class="sub">message: ${m.outreach_model ? esc(m.outreach_model) : 'same model'}</div>
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
    <label class="sub" for="m-omodel">Message model <span class="sub">(optional)</span></label>
    <input id="m-omodel" style="width:100%;margin:5px 0 4px" value="${esc(m.outreach_model || '')}"
           placeholder="blank = use the model above">
    <p class="hint" style="margin:0 0 14px">A smaller model that only writes the
      email + WhatsApp text. Blank means it uses the model above. Set this to
      cut the token cost of drafting.</p>
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
    outreach_model: $('#m-omodel').value,
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
  const want = parseInt($('#howmany') && $('#howmany').value, 10);
  const q = want > 0 ? ('?limit=' + want) : '';
  try { d = await (await fetch('/api/next-up' + q)).json(); } catch { return; }
  const el = $('#nextup');
  if (!el || d.error || !d.queues) { if (el) el.innerHTML = ''; return; }

  const label = {research: 'Research', 'site-brief': 'Write briefs'};
  const parts = [];
  for (const stage of ['research', 'site-brief']) {
    const rows = d.queues[stage] || [];
    if (!rows.length) continue;
    const items = rows.map(r => {
      const star = r.social ? ' <b title="has a Facebook or Instagram page">&#9733;</b>' : '';
      const score = r.rating ? ` <span class="sub">${esc(r.rating)}&#9733; &middot; ${esc(r.reviews || '0')}</span>` : '';
      return `<li style="margin:3px 0">${esc(r.name)}${star}${score}
        <button class="link" title="Never work on this lead again"
          onclick="dropLead('${esc(r.lead_id)}', this)">not this one</button></li>`;
    }).join('');
    parts.push(`<div style="margin-top:8px"><b>${label[stage]} would take these ${rows.length}:</b>
      <ul style="margin:4px 0 0 18px;padding:0">${items}</ul></div>`);
  }
  const capped = d.shown >= d.cap
    ? ` Showing the first ${d.cap}; the rest run as they come.` : '';
  el.innerHTML = parts.length
    ? parts.join('') +
      '<div style="margin-top:8px">&#9733; has a social page, so it goes first. ' +
      'This list follows <b>How many</b> above, so it is exactly who would run.' +
      capped + ' <b>not this one</b> drops a lead from every queue permanently ' +
      '&mdash; nothing is deleted from your Sheet, it is marked Disqualified.</div>'
    : '';
}

async function dropLead(leadId, btn) {
  btn.disabled = true;
  btn.textContent = 'dropping\u2026';
  let d;
  try {
    d = await (await fetch('/api/drop', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: leadId})
    })).json();
  } catch { d = {error: 'Could not reach the dashboard'}; }

  if (!d.ok) {
    btn.disabled = false;
    btn.textContent = 'not this one';
    alert(d.error || 'Could not drop that lead');
    return;
  }
  // Redraw rather than just hiding the row: dropping one pulls the next lead
  // up into the list, and seeing that happen is the point of the preview.
  await drawNextUp();
  await refresh();
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
    <div class="row" style="margin-top:6px">
      <button onclick="suggestAreas()" id="suggest-btn">Suggest areas from the location</button>
      <span class="hint" id="suggest-note" style="margin-left:8px"></span>
    </div>
    <p class="hint">Google gives about 60 results per area and no more, so one area
      runs dry in a day or two. List several and each run moves down the list,
      skipping businesses you already have.</p>
    <p class="hint"><b>Suggest</b> fills the box from whatever is in
      <b>Main location</b> above &mdash; type <i>New Orleans, LA, USA</i> and it
      writes out the neighbourhoods. They are suggestions, so read the list and
      delete the ones you do not want before saving. Nothing is saved until you
      press Save.</p>
    <label class="sub" for="t-sender">Who the message is from</label>
    <input id="t-sender" style="width:100%;margin:5px 0 4px"
           value="${esc(target.sender_name || '')}" placeholder="Lyon Ming">
    <p class="hint">The name on every email and WhatsApp message. Edit it here
      rather than in the campaign file &mdash; once settings have been saved the
      file that actually runs is a private copy, so changing the tracked one
      does nothing.</p>
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

async function suggestAreas() {
  const loc = $('#t-loc').value.trim();
  const note = $('#suggest-note');
  const btn = $('#suggest-btn');
  if (!loc) { note.textContent = 'Type a main location first.'; return; }

  btn.disabled = true;
  note.textContent = 'Asking\u2026';
  try {
    const r = await fetch('/api/suggest-areas', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({location: loc, count: 20})
    });
    const d = await r.json();
    if (!r.ok) { note.textContent = d.error || 'Could not suggest areas.'; return; }

    const box = $('#t-areas');
    // Merge, never overwrite: anything already typed is kept, and a
    // suggestion that duplicates it is dropped. Losing a hand-written list
    // to a button press would be the worst thing this could do.
    const have = box.value.split('\\n').map(s => s.trim()).filter(Boolean);
    const seen = new Set(have.map(s => s.toLowerCase()));
    const added = (d.areas || []).filter(a => !seen.has(a.trim().toLowerCase()));
    box.value = have.concat(added).join('\\n');
    note.textContent = added.length
      ? 'Added ' + added.length + ' \u2014 edit or delete any, then Save.'
      : 'Nothing new to add.';
  } catch (e) {
    note.textContent = 'Could not reach the server.';
  } finally {
    btn.disabled = false;
  }
}

async function saveTarget() {
  const body = {
    category: $('#t-cat').value,
    location: $('#t-loc').value,
    areas: $('#t-areas').value.split('\\n').map(s => s.trim()).filter(Boolean),
    sender_name: $('#t-sender').value.trim(),
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
    <details open>
      <summary class="sub">The full brief &mdash; read it before approving</summary>
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
    </div>
    <div class="row" style="margin-top:10px">
      <input id="rn" placeholder="Notes for the rewrite &mdash; more upmarket, focus on weddings&hellip;"
             onkeydown="if(event.key==='Enter'){event.preventDefault();regenNotes()}">
      <button onclick="regenNotes()">Rewrite with notes</button>
    </div>
    <p class="hint">Not happy with it? <b>Rewrite this brief</b> asks again from
      scratch &mdash; the model is not deterministic, so you get a different
      take, not the same one back. Or type what to change in the box above
      (&ldquo;more upmarket&rdquo;, &ldquo;focus on the wedding packages&rdquo;,
      &ldquo;less formal, it is a family shop&rdquo;) and press <b>Rewrite with
      notes</b>. One model call either way; your notes are used once, not
      remembered.</p>
    <p class="hint">Keys: <b>A</b> approve &middot; <b>S</b> skip &middot;
      <b>&rarr;</b> decide later &middot; <b>R</b> rewrite &middot; <b>N</b> add
      notes. Approving queues it for step 3. Nothing is spent until you
      build it there.</p>
  </div>`;
}

function regenNotes() {
  const el = document.getElementById('rn');
  if (!el) return;
  const notes = el.value.trim();
  if (!notes) { el.focus(); return; }   // empty -- point him at the box, spend nothing
  regen(notes);
}

// Single-key review. Skipped while he is typing in the notes box or selecting
// text in the brief, and whenever a modifier key is held -- so Ctrl+S (save
// page) and Ctrl+R (reload) keep their normal meaning.
document.addEventListener('keydown', (e) => {
  if (!review.length) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === 'a') { e.preventDefault(); decide('approved'); }
  else if (k === 's') { e.preventDefault(); decide('skip'); }
  else if (k === 'arrowright') { e.preventDefault(); move(1); }
  else if (k === 'r') { e.preventDefault(); regen(''); }
  else if (k === 'n') {
    e.preventDefault();
    const el = document.getElementById('rn');
    if (el) el.focus();
  }
});

async function regen(notes) {
  const q = review[at];
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

async function loadReach() {
  const el = $('#reach');
  let d;
  try { d = await (await fetch('/api/reachability')).json(); } catch { return; }
  if (d.error) { el.innerHTML = `<span class="err">${esc(d.error)}</span>`; return; }

  const c = d.counts || {};
  if (!c.verified && !c.rejected && !c.waiting) {
    el.innerHTML = '<div class="what">Nothing researched yet.</div>' +
      '<p class="hint">Run <b>Research</b> and the WhatsApp check runs with it.</p>';
    return;
  }

  const rows = list => list.map(e =>
    `<tr><td style="padding:2px 10px 2px 0">${esc(e.name)}</td>` +
    `<td style="white-space:nowrap;padding:2px 0">${esc(e.dialled || e.phone)}</td>` +
    `<td style="padding:2px 0 2px 10px">${e.email ? 'has email' : ''}</td></tr>`
  ).join('');

  // Only the ones needing a decision are shown outright. The verified and
  // rejected lists are the long ones and are answered questions, so they
  // fold away -- the counts are what Lyon actually reads each morning.
  const fold = (label, list) => list.length
    ? `<details style="margin-top:8px"><summary class="sub">${label} (${list.length})</summary>` +
      `<table style="width:100%;border-collapse:collapse;margin-top:6px">${rows(list)}</table></details>`
    : '';

  el.innerHTML =
    `<div class="what">${c.verified} on WhatsApp &middot; ${c.rejected} not &middot; ` +
    `${c.waiting} not yet checked</div>` +
    `<p class="hint"><b>${c.buildable}</b> lead(s) can be followed up &mdash; those on ` +
    `WhatsApp, plus any with an email address. Those are the ones that go on to a ` +
    `brief. Nothing here has been messaged.</p>` +
    (c.waiting
      ? '<p class="hint"><b>Not yet checked</b> means the relay could not answer. ' +
        'Pair WhatsApp below, then press <b>Check WhatsApp</b>. They are held until ' +
        'then &mdash; no site is built for a lead we cannot reach.</p>' +
        `<table style="width:100%;border-collapse:collapse;margin-top:8px">` +
        `${rows(d.waiting || [])}</table>`
      : '') +
    fold('On WhatsApp', d.verified || []) +
    fold('Not on WhatsApp', d.rejected || []);
}

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
        ${SITE_BUILDER_URL
          ? `<a href="${SITE_BUILDER_URL}" target="_blank" rel="noopener"><button>Open ${SITE_BUILDER}</button></a>`
          : `<button>Open ${SITE_BUILDER}</button>`}
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
  document.getElementById('c' + i).textContent = `Copied. Paste it into ${SITE_BUILDER}.`;
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
loadReach();
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
