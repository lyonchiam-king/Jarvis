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
import hmac
import secrets as _secrets
from collections import deque
import os
from dataclasses import dataclass, field
from urllib.parse import quote
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.exceptions import NotFound

from . import paths, schema, scheduler
# ConfigError is imported here, not inside each view. Four endpoints revert a
# campaign edit by catching it, and in two of them -- /api/angle and
# /api/design-level -- it was never in scope at all: the revert line raised
# NameError instead, so a save that produced an unloadable campaign returned a
# 500 and left that file on disk, which is the one outcome the revert exists
# to prevent. The page could only report it as a transport failure.
from .config import Config, ConfigError, load_config
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
    "visual-brief": ["run", "visual-brief"],
    "site-brief-rewrite": ["run", "site-brief", "--rewrite"],
    "site-build": ["run", "site-build"],
    "site-generate": ["run", "site-generate"],
    "publish": ["run", "publish"],
    "outreach-draft": ["run", "outreach-draft"],
    "preflight": ["preflight"],
    "test-model": ["test-model"],
    "preview-leads": ["run", "discovery", "--dry-run"],
    "preview-messages": ["run", "outreach-draft", "--dry-run"],
}

FULL_RUN = ["discovery", "research", "site-brief", "outreach-draft"]


def full_run_for(config) -> list[str]:
    """The Full pipeline for this campaign's track.

    FULL_RUN above is the original and stays exactly what it was, because a
    campaign that pitches a page must run what it always ran. A campaign that
    pitches a graphic needs the image prompts written in the same pass -- a
    stage nobody presses never runs, which is how reachability shipped and
    did nothing for weeks.

    "visual" drops the site brief: that stage's whole output is a page this
    campaign never links to, and writing one per lead is a model call spent
    on something nothing will read. "both" keeps both, in that order, because
    the message carries the link as well as the picture.
    """
    track = getattr(config.outreach, "track", "website")
    if track == "visual":
        return ["discovery", "research", "visual-brief", "outreach-draft"]
    if track == "both":
        return [
            "discovery", "research", "site-brief", "visual-brief",
            "outreach-draft",
        ]
    return list(FULL_RUN)

# Stages that accept `--limit N`. `preflight` just checks connections, so a
# number would be meaningless there.
TAKES_LIMIT = {
    "discovery",
    "publish",
    "discovery-refresh",
    "research",
    "reachability",
    "reachability-recheck",
    "site-brief",
    "site-brief-rewrite",
    "visual-brief",
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
    "visual-brief",
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

# Offered in the settings field, never written to a campaign by this code.
# Nothing in this container can reach that host to check it -- egress is
# blocked -- so it is a suggestion sitting in front of the user before they
# press Save, which is the difference between them confirming an address and
# us guessing one. A wrong URL is a dead button, and a dead button reads as
# the tool being broken rather than as a setting being wrong.
SUGGESTED_IMAGE_TOOL_URL = "https://labs.google/fx/tools/flow"

# How many names the Next-up preview will render. The point is to curate the
# run before it happens, so it follows the How-many box -- but sixty names is
# a wall, not a list, so it stops here and says so.
PREVIEW_CAP = 25

# Long enough to find the right Google account, read the consent screen and
# think about it; short enough that a tab someone closed does not hold a
# request thread until the dashboard is restarted.
CONSENT_TIMEOUT = 300

# One Sheet creation at a time. The guard that refuses a second Sheet is
# checked before a consent flow that then blocks for minutes, so two presses
# arriving in that window would both pass it -- and Flask serves them on
# separate threads. The button disables itself, but a page reload mid-consent
# brings it back.
_create_sheet_lock = threading.Lock()


def stage_args(
    stage: str,
    limit: int | None = None,
    *,
    social_first: bool | None = None,
    extra: dict[str, list[str]] | None = None,
) -> list[str]:
    """CLI arguments for a stage, with the per-run options applied.

    ``extra`` is keyed by stage name rather than being one flat list, so a
    flag meant for one stage cannot land on another when the Full pipeline
    runs several. Its values are built server-side from validated choices --
    nothing a browser sends reaches an argv unchecked.
    """
    args = list(STAGE_COMMANDS[stage])
    if limit and stage in TAKES_LIMIT:
        args += ["--limit", str(limit)]
    if extra:
        args += list(extra.get(stage, ()))
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


# Where the built React page lives. Same origin on purpose: served from
# anywhere else it would need CORS, a public address and a second way to sign
# in, while from here the login cookie, the API and the page are one thing and
# `Dashboard.bat` needs no new steps.
#
# Module level rather than a closure local because the front door reads it to
# decide which dashboard to open, and a test has to be able to point it at a
# directory with nothing in it -- which is what a checkout without the built
# files actually is.
WEB_ROOT = Path(__file__).parent / "web"


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
        extra: dict[str, list[str]] | None = None,
    ) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = label
            self.lines.clear()
        threading.Thread(
            target=self._run, args=(stages, limit, social_first, extra), daemon=True
        ).start()
        return True

    def _run(
        self,
        stages: list[str],
        limit: int | None = None,
        social_first: bool | None = None,
        extra: dict[str, list[str]] | None = None,
    ) -> None:
        try:
            for stage in stages:
                args = stage_args(
                    stage, limit, social_first=social_first, extra=extra
                )
                self.lines.append(f"$ outreach {' '.join(args)}")
                process = subprocess.Popen(
                    [sys.executable, "-m", "outreach", *args],
                    # No stdin: a stage that tries to prompt (spending
                    # credits, say) would otherwise inherit this console and
                    # wait on a question the page never shows. With the null
                    # device the CLI's confirm helper sees no terminal and
                    # skips instead of hanging the run.
                    stdin=subprocess.DEVNULL,
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
        # Unpacked once, by name. The key grew a fourth element when the
        # service account arrived, and every `self._key[3]` elsewhere would
        # be one more place to get an index wrong -- the same positional
        # hazard the Sheet itself has. A short key is still accepted so the
        # service account is simply absent, which is what it means.
        self._key = key
        self._client_file = key[0]
        self._token_file = key[1]
        self._sheet_id = key[2]
        self._service_account_file = key[3] if len(key) > 3 else None
        self._local = threading.local()
        # Building credentials can refresh and rewrite the token file, so two
        # threads arriving at once must not both do it.
        self._build_lock = threading.Lock()

    def _client(self):
        existing = getattr(self._local, "client", None)
        if existing is not None:
            return existing
        with self._build_lock:
            from .auth import sheets_credentials
            from .sheets import SheetsClient

            # allow_consent=False: nobody is watching a terminal here. Consent
            # opens a browser and blocks until it is completed, so an expired
            # token would hang this request thread rather than fail it, and
            # the page would just stop answering. A service account never
            # reaches that path at all, which is the point of it.
            creds = sheets_credentials(
                self._client_file,
                self._token_file,
                self._service_account_file,
                allow_consent=False,
            )
            client = SheetsClient(creds, self._sheet_id)
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
    # The service account file is part of the key: dropping one in is how
    # the credential changes, and a cached client built on the old sign-in
    # would go on failing with a key sitting right there on disk.
    key = (
        config.secrets.oauth_client_file,
        config.secrets.token_file,
        config.secrets.sheet_id,
        config.secrets.service_account_file,
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



def _explain_sheets(exc: Exception, config: Config) -> str:
    """What to show when the Sheet refuses.

    The commonest failure right after installing a service account is
    forgetting to share the spreadsheet with it, and Google reports that as
    a flat permission error that reads like a broken key.
    """
    from .auth import service_account_email, uses_service_account
    from .sheets import explain

    robot = (
        service_account_email(config.secrets.service_account_file)
        if uses_service_account(config.secrets.service_account_file)
        else ""
    )
    return explain(exc, robot_email=robot, sheet_id=config.secrets.sheet_id)


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


LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Sign in</title>
<style>
  :root { color-scheme: light dark; --bg:#fbfaf9; --ink:#1a1714; --line:#e3ded7;
          --accent:#b4541f; --muted:#6f6862; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#151311; --ink:#efeae4; --line:#2f2a26; --accent:#e08a4e; --muted:#a49a90; }
  }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100dvh; display:flex; align-items:center;
         justify-content:center; background:var(--bg); color:var(--ink);
         font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
         padding:24px calc(20px + env(safe-area-inset-left)) calc(24px + env(safe-area-inset-bottom)); }
  form { width:100%; max-width:340px; }
  h1 { font-size:20px; margin:0 0 6px; }
  p { color:var(--muted); font-size:14px; margin:0 0 18px; }
  /* 16px minimum: Safari zooms the whole page on focus below that, and the
     zoom does not come back on its own. */
  input { width:100%; font-size:16px; padding:13px 14px; border-radius:10px;
          border:1px solid var(--line); background:transparent; color:var(--ink); }
  button { width:100%; margin-top:12px; font-size:16px; padding:14px;
           border-radius:10px; border:1px solid var(--accent);
           background:var(--accent); color:#fff; font-weight:600; }
  .err { color:var(--accent); font-size:14px; margin:12px 0 0; }
</style>
<form method="post">
  <input type="hidden" name="next" value="{{ next }}">
  <h1>SME Outreach</h1>
  <p>This dashboard can spend money and message businesses, so it asks first.</p>
  <input type="password" name="password" placeholder="Dashboard password"
         autocomplete="current-password" autofocus>
  <button type="submit">Sign in</button>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
</form>
"""


# The endpoints reachable without signing in. `login` is obvious; `healthz`
# is here because a platform's health check has no session and never will,
# and a 302 to the login form reads to it as a dead app. Both are named
# rather than matched by path prefix: a prefix would quietly exempt anything
# added under it later.
_NO_LOGIN = frozenset({"login", "healthz"})


def create_app(config: Config) -> Flask:
    app = Flask(__name__)
    runner = Runner()

    # Whether this process published itself to the internet. It changes two
    # things that must not be guessed: which address a login attempt is
    # counted against, and whether the session cookie may only travel over
    # https. `serve` sets it before the first request.
    state: dict = {"config": config, "tunnelled": False}
    # Exposed so `serve` can record that it published this process. Reaching
    # into the closure from outside is worse than one named handle.
    app.config["state"] = state
    # `serve` needs it to schedule the morning batch, and the Runner's own
    # refusal-while-busy is what stops a scheduled run stacking on top of one
    # a person started. Handed over by name rather than reached for through
    # the closure.
    app.config["runner"] = runner

    # Signed-cookie sessions, with a per-process key. Restarting the
    # dashboard signs every device out, which is the right trade for a tool
    # that is started and stopped by hand: no secret to store, and a laptop
    # that was closed does not leave a phone logged in for ever.
    app.secret_key = _secrets.token_urlsafe(32)

    # The session cookie is the whole authentication once you are signed in.
    # HttpOnly keeps script away from it and Lax stops another site sending
    # an authenticated request on your behalf; both are right on a LAN too.
    # `Secure` is set only under a tunnel, because a cookie marked Secure is
    # never sent over plain http and would silently break the local page.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    # Failed attempts, so a password cannot be guessed at machine speed by
    # anything else on the network. Keyed by source address, bounded.
    login_failures: dict[str, list[float]] = {}
    LOGIN_WINDOW = 300.0
    LOGIN_MAX = 8

    def client_ip() -> str:
        """Who is really asking, for rate-limiting purposes.

        Behind a tunnel `remote_addr` is Cloudflare's edge and identical for
        every visitor, so keying on it would let one attacker exhaust the
        login budget for everybody -- the owner included. `CF-Connecting-IP`
        carries the real address.

        It is trusted **only** when this process opened a tunnel itself.
        Served on a LAN, anyone could send that header and get a fresh
        allowance per made-up value, which is worse than not checking.
        """
        if state["tunnelled"]:
            forwarded = request.headers.get("CF-Connecting-IP", "").strip()
            if forwarded:
                return forwarded
        return request.remote_addr or "unknown"

    def _too_many_attempts(who: str) -> bool:
        now = time.time()
        recent = [t for t in login_failures.get(who, []) if now - t < LOGIN_WINDOW]
        login_failures[who] = recent
        if len(login_failures) > 200:  # bounded: a table, not a log
            login_failures.clear()
        return len(recent) >= LOGIN_MAX

    def safe_next(raw: str | None) -> str:
        """Where to go after signing in -- but only somewhere on this site.

        The value arrives in a query string, so anyone who can get a link in
        front of you chooses it. Anything but a plain path on this host is
        discarded rather than corrected: `//example.com` and `/\\example.com`
        are both read by browsers as *another site*, which would turn the
        login page into a redirector that arrives from a trusted address.
        """
        if not raw or not raw.startswith("/"):
            return "/"
        if raw.startswith("//") or raw.startswith("/\\"):
            return "/"
        return raw

    @app.route("/login", methods=["GET", "POST"])
    def login():
        wanted = current().secrets.dashboard_password
        if not wanted:
            # Nothing to sign in to. Say so rather than showing a box that
            # accepts anything.
            return redirect("/")

        # Where the person was heading before being stopped. Losing it sent
        # everyone to the old dashboard however they arrived -- so asking for
        # /app and signing in landed you on / , which looks exactly like the
        # new page never shipped.
        target = safe_next(
            request.form.get("next") if request.method == "POST"
            else request.args.get("next")
        )

        error = ""
        if request.method == "POST":
            who = client_ip()
            if _too_many_attempts(who):
                error = "Too many attempts. Wait a few minutes."
            else:
                given = (request.form.get("password") or "").strip()
                # compare_digest, not ==: a plain comparison returns faster on
                # a wrong first character, which is measurable over a network.
                if hmac.compare_digest(given, wanted):
                    session["in"] = True
                    session.permanent = True
                    return redirect(target)
                login_failures.setdefault(who, []).append(time.time())
                error = "Wrong password."
        return render_template_string(LOGIN_PAGE, error=error, next=target), (
            200 if request.method == "GET" else 401
        )

    @app.before_request
    def _require_login():
        # No password set means no login -- which is safe only on localhost,
        # and `serve` refuses to bind anywhere else without one.
        if not current().secrets.dashboard_password:
            return None
        if session.get("in") or request.endpoint in _NO_LOGIN:
            return None
        # An API call gets JSON it can act on; a page gets the login form.
        # A browser handed JSON shows raw text and a phone shows nothing.
        if request.path.startswith("/api/"):
            return jsonify({"error": "Signed out. Reload the page."}), 401
        return redirect(f"/login?next={quote(request.full_path.rstrip('?'), safe='/?=&')}")

    # The campaign target is editable from the page, so config is state rather
    # than a constant. Every handler reads through `current()` so a save takes
    # effect immediately instead of at the next restart.
    def current() -> Config:
        return state["config"]

    @app.route("/")
    def index():
        """Whichever dashboard this install opens on.

        `/app` was additive while it had never run on Lyon's machine: a bad
        first render would have cost him a morning, and the working page
        staying put was the whole safety net. It has now been the one he
        works from for a week, so it is what the front door opens onto -- and
        the old page keeps its own address rather than being deleted, because
        the safety net is only a safety net while it is reachable.

        `OUTREACH_CLASSIC_DASHBOARD` turns that round for anyone who wants
        the old one back, read on every request rather than captured at
        import so it can be set without a reinstall.
        """
        if _classic_requested() or not (WEB_ROOT / "index.html").exists():
            return _classic_page()
        return redirect(url_for("spa_index"))

    @app.route("/classic")
    def classic_index():
        """The original page, at an address that does not move."""
        return _classic_page()

    def _classic_page():
        return render_template_string(
            PAGE,
            campaign=current().campaign,
            build=current().build,
            whatsapp_enabled=current().outreach.whatsapp_enabled,
        )

    def _classic_requested() -> bool:
        value = (os.environ.get("OUTREACH_CLASSIC_DASHBOARD") or "").strip()
        # A blank value counts as unset, the same rule `OUTREACH_STATE_DIR`
        # follows: Docker passes empty strings through for every variable
        # nobody filled in.
        return value.casefold() in {"1", "true", "yes", "on"}

    @app.route("/manifest.webmanifest")
    def manifest():
        """What "Add to Home Screen" reads to make this an app.

        `standalone` is the part that matters: opened from the icon the page
        gets the whole screen with no address bar, which is the difference
        between an app and a bookmark.
        """
        return jsonify(_manifest("SME Outreach", "Outreach", "/"))

    def _manifest(name: str, short: str, start: str, scope: str | None = None) -> dict:
        """One manifest shape, two pages.

        The start_url is what the icon opens, so each page needs its own --
        a shared manifest would send both home-screen icons to whichever
        address it happened to name, and the second icon would be a
        duplicate of the first.
        """
        return {
            "name": name,
            "short_name": short,
            "start_url": start,
            "scope": scope or start,
            "display": "standalone",
            "background_color": "#f6f5f2",
            "theme_color": "#f6f5f2",
            "icons": [
                {"src": "/icon.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "any maskable"},
            ],
        }

    @app.route("/healthz")
    def healthz():
        """Is this process alive? Nothing more.

        A platform restarts a deployment whose health check does not answer
        200, and with a password set every other path on this app answers a
        302 to /login -- so without an exempt route the check reads a
        perfectly healthy app as broken and restarts it for ever, which
        looks like a crash loop caused by something else entirely.

        It reports liveness and nothing else. Not the campaign, not which
        keys are set, not a lead count: this is the one URL on the whole app
        that anybody can reach, and every one of those is a fact about the
        business that an unauthenticated caller has no claim to. It also
        touches no Sheet and no API -- a check that does real work fails
        when Google has a bad minute, and the platform's answer to that is
        to kill a process that was never at fault.
        """
        return jsonify({"ok": True})

    @app.route("/favicon.ico")
    def favicon():
        """Browsers ask for this unprompted, on every page, always.

        Without it every visit logged a 404 -- which is noise in itself, and
        worse, noise of exactly the shape a real broken asset would make. The
        page icon already exists; this points at it rather than adding a
        second one.
        """
        return redirect("/icon.png")

    @app.route("/icon.png")
    def icon():
        """The Home Screen icon, drawn here rather than shipped as a file.

        A binary in the repository would be one more thing to keep in step
        with the palette, and this is a flat mark: the accent colour with a
        rounded square knocked out of it. Written by hand as a minimal PNG so
        nothing new has to be installed to draw it.
        """
        import struct
        import zlib

        size = 512
        bg = (0xB4, 0x55, 0x2D)      # --accent
        fg = (0xF6, 0xF5, 0xF2)      # --bg, the mark
        # A thick ring: an O for Outreach, legible at 60px on a home screen.
        cx = cy = size / 2
        outer, inner = size * 0.34, size * 0.20

        rows = bytearray()
        for y in range(size):
            rows.append(0)  # PNG filter byte: none
            for x in range(size):
                d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                rows.extend(fg if inner <= d <= outer else bg)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + chunk(b"IEND", b"")
        )
        return app.response_class(png, mimetype="image/png")

    @app.route("/guide")
    def guide():
        from .guide import guide_html

        return render_template_string(GUIDE_PAGE, body=guide_html())

    # ------------------------------------------------------- the newer page
    #
    # A second front end, built from React sources in `frontend/` and served
    # from disk as plain static files. It lives at /app rather than / so the
    # hand-written page stays exactly where it is: this one is new, nothing
    # about it is proven on Lyon's machine yet, and a dashboard that spends
    # money is not the place to find out by replacing the working one.
    #
    @app.route("/app")
    @app.route("/app/")
    def spa_index():
        index = WEB_ROOT / "index.html"
        if not index.exists():
            # Said plainly, because the only way this happens is a checkout
            # without the built files, and "404" would send someone hunting
            # for a route that is present.
            return (
                "The new dashboard was not built into this copy. "
                "The old one is still at /.",
                404,
            )
        return send_from_directory(WEB_ROOT, "index.html")

    @app.route("/app/manifest.webmanifest")
    def spa_manifest():
        return jsonify(_manifest("SME Outreach", "Outreach", "/app/", scope="/app"))

    @app.route("/app/<path:asset>")
    def spa_asset(asset: str):
        """Serve one built file, and nothing outside the build directory.

        `send_from_directory` resolves the path and refuses anything that
        escapes the root, so a crafted `../../.env` gets a 404 rather than a
        secret. That guarantee is the reason this does not join paths itself.
        """
        try:
            return send_from_directory(WEB_ROOT, asset)
        except NotFound:
            # A single-page app owns its own routing: /app/leads is a screen,
            # not a file. Anything that is not a real file and does not look
            # like one gets the page, which then reads the address itself.
            if "." in Path(asset).name:
                raise
            return spa_index()

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
                # Where the compliance footer says the message is sent from.
                # Shown, not edited here: it is a legal setting, and every
                # lead's own country is read off its number anyway.
                "country_code": c.compliance.country_code,
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
            set_key(paths.env_file(), "CAMPAIGN_CONFIG", str(local))
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
                "key_set": bool(env_name) and is_set(paths.env_file(), env_name),
                # Presence only, never the value -- same rule as every other
                # key on this page.
                "places_key_set": bool(c.secrets.places_api_key),
                "guaranteed": c.research.provider == "anthropic",
            }
        )

    @app.route("/api/model/catalogue")
    def api_model_catalogue():
        """Which models this campaign's own key can actually use.

        The rule has always been that no model name may be written into this
        code, because a name correct today 404s in a month. What was missing
        was the other half: a way for the owner to *ask*. `outreach
        list-models` speaks only to Gemini, for the site builder, and it is a
        terminal command -- so when the model his campaign named was retired,
        the tool said what was wrong and he had nowhere to look up the answer.

        Never cached. A catalogue is exactly the thing that changes between
        the time it was fetched and the moment it is wrong.
        """
        from .providers.llm import LLMError, available_models

        c = current()
        try:
            names = available_models(c)
        except LLMError as exc:
            return jsonify({"error": str(exc)}), 200
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({
            "provider": c.research.provider,
            "models": names,
            "model": c.research.model,
            "outreach_model": c.outreach.model,
        })

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
        # The Places key rides along on the same save, because the setup
        # checklist asks for both keys on one screen and two save buttons for
        # one action is how a half-configured install happens.
        places_key = (payload.get("places_key") or "").strip()

        if provider is not None and provider not in PROVIDER_DEFAULTS:
            return jsonify({"error": f"Unknown provider {provider!r}"}), 400

        # The key first: if this fails, the campaign file is still untouched.
        if api_key.strip():
            target = provider or current().research.provider
            env_name = PROVIDER_KEY_ENV.get(target)
            if not env_name:
                return jsonify({"error": f"{target} takes no API key"}), 400
            try:
                set_key(paths.env_file(), env_name, api_key.strip())
                os.environ[env_name] = api_key.strip()
            except EnvEditError as exc:
                return jsonify({"error": str(exc)}), 200

        if places_key:
            try:
                set_key(paths.env_file(), "PLACES_API_KEY", places_key)
                os.environ["PLACES_API_KEY"] = places_key
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
        from .config import PROVIDER_KEY_ENV
        from .env_edit import is_set

        missing = [
            name
            for name, value in (
                ("SHEET_ID", c.secrets.sheet_id),
                ("PLACES_API_KEY", c.secrets.places_api_key),
            )
            if not value
        ]
        # Which model key matters depends on the provider in use. The old
        # list hardcoded ANTHROPIC_API_KEY, so an Ollama run was told it was
        # "not set up" over a key it should never have been asked for, and an
        # OpenRouter run was waved through while its own key was missing.
        env_name = PROVIDER_KEY_ENV.get(c.research.provider, "")
        if env_name and not is_set(paths.env_file(), env_name):
            missing.append(env_name)
        if missing:
            return jsonify({"setup_needed": missing})

        try:
            summary = _load_summary(c)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200
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
        from .stage import Stage, advance

        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        preview_url = (payload.get("preview_url") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400
        if preview_url and not preview_url.startswith(("http://", "https://")):
            return jsonify({"error": "preview URL must start with http"}), 400

        try:
            sheets = _sheets_client(current())
            # Same gate as the CLI's briefs-record: a disqualified lead's
            # build must not be recorded, or they drop straight back into
            # the outreach queue with a live site behind the message.
            leads = sheets.read_rows(schema.LEADS)
            entry = leads.get(lead_id)
            current_stage = (entry[1].get("Stage") if entry else "") or ""
            if current_stage.strip() == Stage.DISQUALIFIED.value:
                return jsonify(
                    {"error": "This lead is disqualified -- the build will not "
                    "be recorded for it."}
                ), 200
            sheets.update_column(
                schema.SITE_BRIEFS, "Lovable Project ID", {lead_id: "built-by-hand"}
            )
            if preview_url:
                sheets.update_column(
                    schema.SITE_BRIEFS, "Preview URL", {lead_id: preview_url}
                )
            # advance(), not a bare Stage value: never move a lead backwards.
            sheets.update_column(
                schema.LEADS,
                "Stage",
                {lead_id: advance(current_stage, Stage.SITE_BUILT).value},
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
            started = runner.start(
                "Full pipeline", full_run_for(current()), limit, social_first
            )
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

    @app.route("/api/design-level", methods=["GET", "POST"])
    def api_design_level():
        """Read or set how much art direction the site prompt carries.

        A craft constant for the whole campaign, not a per-business field --
        every business gets the same answer, so it lives in the campaign file
        as template text rather than costing a schema field per lead.
        """
        from .campaign_edit import CampaignEditError, rewrite_design_level
        from .config import DESIGN_LEVELS

        if request.method == "GET":
            return jsonify({
                "level": current().build.design_level,
                "levels": sorted(DESIGN_LEVELS),
            })

        level = ((request.json or {}).get("level") or "").strip().lower()
        if level not in DESIGN_LEVELS:
            return jsonify({
                "error": f"Unknown design level {level!r}. "
                         f"Expected one of {sorted(DESIGN_LEVELS)}."
            }), 400

        try:
            path = _writable_campaign()
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        try:
            original = path.read_text()
            path.write_text(rewrite_design_level(original, level))
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        except OSError as exc:
            return jsonify({"error": f"Could not save {path}: {exc}"}), 200

        try:
            state["config"] = load_config(path)
        except ConfigError as exc:
            # Same shape as every other campaign edit: put the file back
            # rather than leave a campaign the pipeline cannot load.
            path.write_text(original)
            return jsonify({"error": f"Reverted -- {exc}"}), 200

        return jsonify({"ok": True, "level": level})

    @app.route("/api/angle", methods=["GET", "POST"])
    def api_angle():
        """Read or set which argument this campaign makes.

        Not a wording preference: it changes who discovery keeps and what the
        message claims, so it is one setting rather than three that can
        disagree with each other.
        """
        from .campaign_edit import CampaignEditError, rewrite_angle
        from .config import ANGLES

        if request.method == "GET":
            c = current()
            return jsonify({
                "angle": c.outreach.angle,
                "angles": sorted(ANGLES),
                "offers_follow_up": c.outreach.offers_follow_up,
            })

        payload = request.json or {}
        angle = (payload.get("angle") or "").strip().lower()
        if angle not in ANGLES:
            return jsonify({
                "error": f"Unknown angle {angle!r}. Expected one of {sorted(ANGLES)}."
            }), 400
        follow_up = bool(payload.get("offers_follow_up"))

        # A promise of follow-up belongs to the conversion argument, whose
        # model paragraph is where it is mentioned. The no-website message
        # has nowhere to put it, so saving it there would store a claim that
        # never appears -- and would surface later as a setting that did
        # nothing.
        if follow_up and angle != "conversion":
            return jsonify({
                "error": "Follow-up is only offered by the conversion angle. "
                         "Switch the angle first, or leave it off."
            }), 200

        try:
            path = _writable_campaign()
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        try:
            original = path.read_text()
            path.write_text(rewrite_angle(original, angle, follow_up))
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        except OSError as exc:
            return jsonify({"error": f"Could not save {path}: {exc}"}), 200

        try:
            state["config"] = load_config(path)
        except ConfigError as exc:
            path.write_text(original)
            return jsonify({"error": f"Reverted -- {exc}"}), 200

        return jsonify({"ok": True, "angle": angle, "offers_follow_up": follow_up})

    @app.route("/api/services", methods=["GET", "POST"])
    def api_services():
        """Read or set the other services listed at the end of every message.

        A campaign-level constant, not a per-business field: every lead gets
        the same list. It is here rather than only in the campaign file
        because the wording is the part Lyon will want to tune after reading
        a few replies, and a setting he can only reach by editing YAML is a
        setting he does not have.
        """
        from .campaign_edit import CampaignEditError, rewrite_services

        if request.method == "GET":
            c = current()
            return jsonify({
                "intro": c.outreach.services_intro,
                "services": list(c.outreach.services),
            })

        payload = request.json or {}
        intro = (payload.get("intro") or "").strip()
        raw = payload.get("services")
        if isinstance(raw, str):
            raw = raw.splitlines()
        if not isinstance(raw, list):
            return jsonify({"error": "services must be a list of lines"}), 400
        services = [str(line).strip() for line in raw if str(line).strip()]

        if services and not intro:
            return jsonify({
                "error": "Add the line that introduces the list, or the "
                         "bullets arrive with no explanation."
            }), 200

        try:
            path = _writable_campaign()
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        try:
            original = path.read_text()
            path.write_text(rewrite_services(original, intro, services))
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        except OSError as exc:
            return jsonify({"error": f"Could not save {path}: {exc}"}), 200

        try:
            state["config"] = load_config(path)
        except ConfigError as exc:
            path.write_text(original)
            return jsonify({"error": f"Reverted -- {exc}"}), 200

        return jsonify({"ok": True, "services": services})

    @app.route("/api/whatsapp/status")
    def api_whatsapp_status():
        """Relay connection state + daily counters."""
        c = current()
        if not c.outreach.whatsapp_enabled:
            return jsonify({"enabled": False})
        try:
            from .providers.whatsapp import WhatsAppClient

            port = c.outreach.whatsapp_relay_port
            client = WhatsAppClient(port=port)
            return jsonify({
                "enabled": True,
                "relay_url": f"http://127.0.0.1:{port}",
                **client.get_status(),
            })
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

    @app.route("/api/whatsapp/reset", methods=["POST"])
    def api_whatsapp_reset():
        """Unlink the paired WhatsApp account and offer a fresh QR.

        For switching phones, or re-pairing when the link has gone stale.
        The relay does the work; this exists because a relay endpoint with no
        button in the dashboard may as well not be there -- /logout shipped
        months ago and had never once been called.
        """
        c = current()
        if not c.outreach.whatsapp_enabled:
            return jsonify({"error": "WhatsApp is switched off for this campaign."}), 400

        from .providers.whatsapp import WhatsAppClient, WhatsAppError

        client = WhatsAppClient(port=c.outreach.whatsapp_relay_port)
        try:
            note = client.logout()
        except WhatsAppError as exc:
            return jsonify({"error": str(exc)}), 200
        except Exception as exc:  # noqa: BLE001 -- surfaced on the page
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({"ok": True, "note": note})

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

    def _shot_for(cfg, lead, lead_id, visual_row=None):
        """The one picture this lead's message carries, or None.

        A confirmed graphic wins over a screenshot of the site -- it is what
        the message is written about, and the page it displaces is still in
        the text as a link. One picture, because the relay sends one.

        Both of the screenshot's homes are still checked: beside a page this
        pipeline generated, and in the lead's slug directory when the site
        exists only at a URL.
        """
        from . import screenshot as shot_mod
        from . import visual as visual_lib
        from .stages.outreach_draft import shot_dir_for, site_page_for

        if visual_row is not None:
            try:
                found = visual_lib.confirmed_image(visual_row)
            except Exception:  # noqa: BLE001 -- a picture is never fatal
                found = None
            if found is not None:
                return found

        candidates = []
        page = site_page_for(cfg, lead, lead_id)
        if page is not None:
            candidates.append(shot_mod.shot_path_for(page))
        candidates.append(shot_dir_for(cfg, lead, lead_id) / "screenshot.png")
        for path in candidates:
            if path.exists() and path.stat().st_size:
                return path
        return None

    def _visual_rows(c, sheets=None):
        """Every Visual Briefs row, or {} on the original track.

        A campaign that pitches a page must not pay a Sheet round trip for a
        tab it has nothing in, so the track decides whether this reads at
        all. Never raises: the graphic is decoration over a message that
        sends regardless, and a failed read here must not take the send
        queue down with it.
        """
        from .stages.outreach_draft import needs_an_image

        if not needs_an_image(c):
            return {}
        try:
            return (sheets or _sheets_client(c)).read_rows(schema.VISUAL_BRIEFS)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read the Visual Briefs tab: %s", exc)
            return {}

    def _visual_row_for(c, lead_id, sheets=None):
        entry = _visual_rows(c, sheets).get(lead_id)
        return entry[1] if entry else None

    def _pages_offered(c, sheets=None):
        """Each lead's Preview URL, or {} where no message could mention one.

        Only read on the `visual` track, and only to answer whether a message
        already on the Sheet was written for the *other* pitch. Changing the
        track changes what is drafted next; it cannot reach back into
        messages already written, and those are what step 4 actually sends --
        so a campaign switched to graphics still had a queue full of
        "Website: https://..." with a picture attached. The setting looked
        like it had done nothing.
        """
        if c.outreach.track != "visual":
            return {}
        try:
            rows = (sheets or _sheets_client(c)).read_rows(schema.SITE_BRIEFS)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read the Site Briefs tab: %s", exc)
            return {}
        return {
            lead_id: url
            for lead_id, (_n, row) in rows.items()
            if (url := (row.get("Preview URL") or "").strip())
        }

    def _written_for_a_page(message: str, preview_url: str) -> bool:
        """Whether this queued message is the website pitch, still.

        Matched on the lead's own Preview URL rather than on the presence of
        any link: a compliance footer or a services block may legitimately
        carry one, and flagging those would train the reader to ignore the
        flag. The site this pipeline built for them is the one thing that
        cannot be in a message that offers only a picture.
        """
        return bool(preview_url) and preview_url in (message or "")

    def _visual_confirmed(entry) -> bool:
        """Whether this ``(row_number, row)`` entry has a usable graphic."""
        from . import visual as visual_lib

        if not entry:
            return False
        try:
            return visual_lib.confirmed_image(entry[1]) is not None
        except Exception:  # noqa: BLE001
            return False

    def _visual_state(row, sent: bool = False) -> str:
        """Where one graphic is up to, in one word the page can branch on.

        Five states, because five things are genuinely different and each
        needs a different button: retired by hand, waiting for the picture,
        waiting for a person to read the words on it, ready to send, and
        gone. `sent` is read off the Outreach Log rather than this tab,
        because the Sheet records a send there and the visual track writes no
        Stage at all -- and a graphic that has reached a business is finished
        work, not a queue item. Leaving it under Ready made that list grow by
        one every time a message went out, which is the opposite of what a
        queue is for.
        """
        from . import visual as visual_lib

        if visual_lib.is_skipped(row):
            return "skipped"
        if visual_lib.resolve_image((row or {}).get("Image File", "")) is None:
            return "needs_image"
        if not visual_lib.text_confirmed(row):
            return "needs_check"
        if sent:
            return "sent"
        return "ready"

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

        path = _shot_for(c, entry[1], lead_id, _visual_row_for(c, lead_id))
        if path is None:
            return jsonify({"error": "No picture for this lead yet"}), 404
        # The graphic arrives as whatever the image tool exported, so the type
        # is read off the name rather than asserted. A PNG served as a JPEG
        # renders in a browser and then fails in the relay, which is the worst
        # place to find out.
        import mimetypes

        guessed = mimetypes.guess_type(path.name)[0] or "image/png"
        return send_file(path.resolve(), mimetype=guessed)

    @app.route("/api/leads")
    def api_leads():
        """The Leads tab, as rows the page can render.

        The old page never needed this -- it worked queue by queue, and a
        lead only appeared when some stage was ready to act on it. A table
        view needs the whole sheet, so this is a plain read with the column
        names mapped to the field names the page uses. Nothing is computed
        here: what the Sheet says is what is shown, which is the only way a
        table and the Sheet can be trusted to agree.
        """
        c = current()
        if not c.secrets.sheet_id:
            return jsonify({"leads": []})
        try:
            rows = _sheets_client(c).read_rows(schema.LEADS)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200

        def number(raw: str) -> float | int:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return 0
            return int(value) if value.is_integer() else value

        leads = [
            {
                "place_id": lead_id,
                "stage": row.get("Stage", ""),
                "business_name": row.get("Business Name", ""),
                "category": row.get("Category", ""),
                "address": row.get("Address", ""),
                "phone": row.get("Phone", ""),
                "rating": number(row.get("Rating", "")),
                "review_count": number(row.get("Review Count", "")),
                "website_status": row.get("Website Status", "") or "none",
                "website_url": row.get("Website URL", ""),
                "maps_url": row.get("Maps URL", ""),
                "campaign": row.get("Campaign", ""),
                "country": row.get("Country", ""),
                "discovered_at": row.get("Discovered At", ""),
                "last_refreshed": row.get("Last Refreshed", ""),
                "priority": row.get("Priority", ""),
                "owner_notes": row.get("Owner Notes", ""),
                "next_action": row.get("Next Action", ""),
                "whatsapp_reachable": row.get("WhatsApp Reachable", "") or "unknown",
                "whatsapp_checked_at": row.get("WhatsApp Checked At", ""),
            }
            for lead_id, (_n, row) in rows.items()
        ]
        return jsonify({"leads": leads})

    @app.route("/api/lead-note", methods=["POST"])
    def api_lead_note():
        """Write your own note against one lead.

        Owner Notes is a manual column, so no stage may touch it. This is
        the exception the rule names: you pressed save on a row you are
        looking at, which is exactly what `user_edit` is for.
        """
        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400
        note = str(payload.get("note") or "")

        try:
            written = _sheets_client(current()).update_column(
                schema.LEADS, "Owner Notes", {lead_id: note}, user_edit=True
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        if not written:
            return jsonify({"error": f"No Leads row for {lead_id}"}), 200
        return jsonify({"ok": True})

    @app.route("/api/test-key", methods=["POST"])
    def api_test_key():
        """Ask the service itself whether a key works.

        A key that is merely *present* proves nothing -- the pipeline has
        failed on a pasted-with-quotes key, a key for the wrong project, and
        a key whose API was never enabled, and each of those only showed up
        as a failed run twenty leads later. So this spends one real request
        and reports what came back.

        The key under test is taken from the request when one is supplied,
        so it can be checked *before* being saved, and is never echoed back
        -- only the verdict is.
        """
        payload = request.json or {}
        kind = (payload.get("type") or "").strip().casefold()
        given = (payload.get("key") or "").strip()
        c = current()

        if kind == "places":
            key = given or c.secrets.places_api_key
            if not key:
                return jsonify({"valid": False, "message": "No Places key to test."})
            from .providers.places import PlacesClient, PlacesError

            try:
                # One page, one result: the smallest question that still
                # proves the key, the project and the enabled API.
                client = PlacesClient(api_key=key)
                next(iter(client.search_text("cafe", page_size=1, max_pages=1)), None)
            except PlacesError as exc:
                return jsonify({"valid": False, "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return jsonify({"valid": False, "message": f"{type(exc).__name__}: {exc}"})
            return jsonify({"valid": True, "message": "Places answered. The key works."})

        if kind == "model":
            from .config import PROVIDER_DEFAULTS, PROVIDER_KEY_ENV
            from .providers.llm import (
                BriefWriter,
                LLMError,
                OpenAICompatibleWriter,
                build_areas_prompt,
            )

            provider = (payload.get("provider") or c.research.provider).strip()
            if provider not in PROVIDER_DEFAULTS:
                return jsonify({"valid": False, "message": f"Unknown provider {provider!r}."})
            model = (payload.get("model") or c.research.model).strip()

            # Built here rather than through `make_writer` so a key can be
            # tested before it is saved -- which is the whole point of a test
            # button. It follows make_writer's shape exactly; if that gains a
            # provider this must too.
            try:
                if provider == "anthropic":
                    writer = BriefWriter(
                        given or c.secrets.anthropic_api_key,
                        model=model,
                        effort=c.research.effort,
                    )
                else:
                    key = given or c.secrets.llm_api_key
                    if provider != "ollama" and not key:
                        return jsonify({
                            "valid": False,
                            "message": f"{PROVIDER_KEY_ENV.get(provider, 'The API key')} "
                                       f"is not set, and none was given to test.",
                        })
                    writer = OpenAICompatibleWriter(
                        key,
                        base_url=c.research.base_url or PROVIDER_DEFAULTS[provider],
                        model=model,
                    )
                # A real round trip that also proves the model can return the
                # JSON shape the pipeline needs. A key that authenticates but
                # whose model cannot produce a schema is not a working setup.
                writer.suggest_areas(build_areas_prompt("Penang, Malaysia", 3))
            except LLMError as exc:
                return jsonify({"valid": False, "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return jsonify({"valid": False, "message": f"{type(exc).__name__}: {exc}"})
            return jsonify({
                "valid": True,
                "message": f"{model} answered, in the right shape.",
            })

        return jsonify({"error": "type must be 'places' or 'model'"}), 400

    @app.route("/api/create-sheet", methods=["POST"])
    def api_create_sheet():
        """Make this person their own Sheet, in their own Google account.

        The first thing a new user needed to do was create a spreadsheet by
        hand, find its ID in the URL, and paste it into a file -- three
        chances to go wrong before anything worked. Signing in already
        proves whose account it is, so the Sheet may as well be made for
        them and the ID written where it belongs.
        """
        if not _create_sheet_lock.acquire(blocking=False):
            return jsonify({
                "error": "Still working on the last one. Finish the Google "
                         "sign-in in the window that opened, or wait a moment "
                         "and press again."
            }), 409
        try:
            return _make_the_sheet()
        finally:
            _create_sheet_lock.release()

    def _make_the_sheet():
        from .auth import AuthError, get_credentials
        from .env_edit import EnvEditError, set_key
        from .sheets import SheetsClient, SheetsError

        c = current()
        # Both, because they can disagree: the reload at the end of a
        # successful run is allowed to fail, and if the guard trusted only
        # the in-memory config a second press would then make a second,
        # empty Sheet -- which is the one thing this guard exists to stop.
        if c.secrets.sheet_id or os.environ.get("SHEET_ID"):
            return jsonify({
                "error": "A Sheet is already set up. Clear SHEET_ID in .env "
                         "first if you really want a new, empty one."
            }), 400

        try:
            # Consent has to be allowed here -- on a first run there is no
            # token to reuse -- but it is bounded, so a sign-in someone walks
            # away from fails the request instead of parking this thread on a
            # callback that will never arrive.
            creds = get_credentials(
                c.secrets.oauth_client_file,
                c.secrets.token_file,
                consent_timeout=CONSENT_TIMEOUT,
            )
        except AuthError as exc:
            return jsonify({"error": str(exc)}), 400

        # Named the way its owner would search for it, not after the
        # campaign slug: this Sheet lands in someone else's Drive alongside
        # everything else they own, and "penang-hair-salons" means nothing
        # there a month later.
        title = f"Outreach -- {c.campaign.category} in {c.campaign.location}"
        try:
            sheets = SheetsClient(creds)
            sheet_id = sheets.create_spreadsheet(title)
            sheets.ensure_tabs(schema.ALL_TABS)
        except SheetsError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:  # noqa: BLE001 -- surfaced on the page
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502

        # Written to .env, not held in memory: the CLI and tomorrow's run
        # both read it from there, and a Sheet nobody can find again would
        # be worse than not creating one.
        try:
            set_key(paths.env_file(), "SHEET_ID", sheet_id)
        except (EnvEditError, OSError) as exc:
            # The Sheet exists at this point, so the URL goes in the message.
            # Losing it here would mean an orphaned spreadsheet and a button
            # that just says it failed.
            return jsonify({
                "error": f"Your Sheet was created -- {sheets.url} -- but the "
                         f"ID could not be saved: {exc}. Put SHEET_ID="
                         f"{sheet_id} in your .env file by hand."
            }), 200

        # Both, and in this order: the env var so this process sees it at
        # once, then a reload so every panel is rebuilt against the new
        # config rather than the one captured at startup.
        os.environ["SHEET_ID"] = sheet_id
        try:
            state["config"] = load_config(current().source_path)
        except Exception:  # noqa: BLE001
            # Every exception, not just ConfigError: the Sheet exists and
            # .env is written by now, so anything raised here is a worse
            # outcome reported than swallowed. A restart picks it up.
            log.warning("Sheet created, but reloading config failed", exc_info=True)

        return jsonify({"ok": True, "url": sheets.url, "title": title})

    @app.route("/api/repair-tabs", methods=["POST"])
    def api_repair_tabs():
        """Add any tab this version needs that the Sheet does not have.

        Every schema addition breaks every existing Sheet the same way: the
        spreadsheet was created from the schema as it stood that day and is
        never touched again, so Google answers a read of the new tab with
        "Unable to parse range" -- which reads as a bug in this tool.

        `ensure_tabs` has always been the repair and has always been
        idempotent. What it did not have was a way to reach it: it lived in
        `outreach init-sheet`, a terminal command, and the person who owns
        this tool does not use a terminal. A fix nobody can perform is not a
        fix.

        It adds only missing tabs and rewrites only row 1. No data row is
        touched, so pressing it twice costs nothing and pressing it on a
        healthy Sheet does nothing at all.
        """
        c = current()
        if not c.secrets.sheet_id:
            return jsonify({
                "error": "There is no Sheet yet. Press Create my Google Sheet "
                         "first."
            }), 200
        try:
            sheets = _sheets_client(c)
            before = set(sheets.existing_tab_titles())
            sheets.ensure_tabs(schema.ALL_TABS)
            after = set(sheets.existing_tab_titles())
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200

        added = sorted(after - before)
        # The read cache is per tab and keyed by title, so a panel that just
        # failed on a missing tab would go on failing from its own snapshot
        # until the TTL ran out -- which is the repair looking like it did
        # nothing.
        _invalidate_sheets_cache()
        return jsonify({
            "ok": True,
            "added": added,
            "note": (
                "Added: " + ", ".join(added)
                if added
                else "Every tab was already there -- nothing to add."
            ),
        })

    # ------------------------------------------------- the infographic track
    #
    # The graphic is generated outside this tool, by hand, in the image tool.
    # So these endpoints are the hand-off in both directions: the prompt out,
    # the picture back. Nothing here generates an image and nothing here can
    # check that the words on one rendered correctly -- which is why the
    # confirmation tick is a gate rather than advice.

    @app.route("/api/visual/queue")
    def api_visual_queue():
        """Every image prompt, with where each one is up to."""
        from . import visual as visual_lib
        from .stages.outreach_draft import needs_an_image

        c = current()
        catalogue = {
            "styles": sorted(visual_lib.STYLES),
            "layouts": sorted(visual_lib.LAYOUTS),
            "chosen_styles": list(c.visual.styles),
            "chosen_layouts": list(c.visual.layouts),
        }
        base = {
            "track": c.outreach.track,
            "enabled": needs_an_image(c),
            "tool_name": c.visual.image_tool_name,
            "tool_url": c.visual.image_tool_url,
            "signature": c.visual.signature,
            "catalogue": catalogue,
            "queue": [],
            "counts": {"needs_image": 0, "needs_check": 0, "ready": 0,
                       "sent": 0, "skipped": 0},
        }
        if not base["enabled"] or not c.secrets.sheet_id:
            return jsonify(base)

        try:
            sheets = _sheets_client(c)
            leads = sheets.read_rows(schema.LEADS)
            briefs = sheets.read_rows(schema.VISUAL_BRIEFS)
            # The message that will actually go out. Read here because this
            # card is where the question gets asked -- the proofreading box
            # beside it was read as the message, which is a mistake you only
            # make once a graphic has reached a real business.
            drafted = sheets.read_rows(schema.OUTREACH_LOG)
        except Exception as exc:  # noqa: BLE001
            from .sheets import TabMissing

            # Flagged rather than left for the page to pattern-match on the
            # wording: an error message is for a person to read, and a page
            # that decided which button to show by searching it would break
            # the next time a sentence was improved.
            return jsonify({
                **base,
                "error": _explain_sheets(exc, c),
                "missing_tab": isinstance(exc, TabMissing),
            }), 200

        # Where step 4 actually stands on each of these, by step 4's own
        # rules. Asked rather than recomputed: a card claiming "ready" beside
        # a queue that does not offer the lead is the one failure a person
        # cannot diagnose from the outside.
        position = _send_position(c, sheets)

        queue = []
        for lead_id, (_n, row) in briefs.items():
            lead = leads.get(lead_id, (0, {}))[1]
            logged = (drafted.get(lead_id) or (0, {}))[1]
            where = position.get(lead_id, {})
            state = _visual_state(row, sent=bool(_sent_already(logged)))
            base["counts"][state] = base["counts"].get(state, 0) + 1
            queue.append(
                {
                    "lead_id": lead_id,
                    "name": row.get("Business Name") or lead.get("Business Name", ""),
                    "category": lead.get("Category", ""),
                    "prompt": row.get("Image Prompt", ""),
                    # The evidence behind each card, so a claim can be checked
                    # against the row that justified it before it is sent.
                    "gaps": row.get("Gaps Used", ""),
                    "style": row.get("Style", ""),
                    "layout": row.get("Layout", ""),
                    "specific": visual_lib.is_specific(row),
                    "state": state,
                    "approval": (row.get("Approval Status") or "").strip(),
                    # The exact words that must appear on the picture, so they
                    # can be read against it side by side. Nothing here can
                    # test that they rendered; this is what makes the check
                    # possible for the one pair of eyes that can.
                    "expected_text": _expected_text(row),
                    # The real WhatsApp text, written per business by the
                    # draft stage. Not the words above, which belong on the
                    # picture and are never sent anywhere.
                    "message": logged.get("WhatsApp Message", ""),
                    "sent_at": logged.get("WhatsApp Sent At", ""),
                    # Whether step 4 is holding this lead, and why -- in step
                    # 4's own words. A confirmed picture whose message never
                    # appears there is the thing he reported, and the card
                    # was the one screen that could not say so.
                    "in_send_queue": bool(where.get("queued")),
                    "send_hold": "" if where.get("queued") else where.get("reason", ""),
                    # Whether the one repair this dashboard has would fix it.
                    # A button offered beside "it has no phone number" would
                    # press and change nothing, which reads as broken.
                    "can_unblock": bool(where.get("fixable")),
                    "created_at": row.get("Created At", ""),
                }
            )
        base["queue"] = queue
        return jsonify(base)

    # Why a lead with a confirmed picture is not in step 4, in words rather
    # than a code. Keyed by the reason step 4 itself records, so the two
    # cannot describe the same lead differently.
    # The two holds **Send again** actually repairs -- it clears the send
    # status and restores Status to Drafted, and nothing else. The other
    # reasons need the lead fixing (a phone number, a Leads row) or are a
    # different channel entirely, and a button promising to undo one of
    # those would not work. Same list step 4's own excluded rows use.
    _UNBLOCKABLE = ("status_not_drafted", "already_sent")

    _SEND_HOLD_WORDS: dict[str, str] = {
        "already_sent": "already sent from step 4",
        "status_not_drafted": "its Outreach Log row does not say Drafted",
        "phone_empty": "there is no phone number on its Leads row",
        "no_lead_row": "it has no row on the Leads tab",
        "phone_not_mobile": "its number cannot receive WhatsApp",
        "unreachable": "it has no email and its number cannot take WhatsApp",
        "email_channel": "it was drafted as an email, so it is in Gmail drafts",
        "unknown_channel": "its Channel cell is not WhatsApp",
    }

    def _sent_already(logged) -> bool:
        """Whether this lead's message has left, by step 4's own test.

        Keyed on `WhatsApp Status` first, because that is what step 4
        excludes on -- `unconfirmed` means the message left and was missing
        from that set once already, which kept a sent lead at the top of the
        queue. The timestamp is a fallback for a row stamped by hand.
        """
        from .providers.whatsapp import WA_SENT_STATUSES

        status = (logged.get("WhatsApp Status") or "").strip().casefold()
        if status in WA_SENT_STATUSES:
            return True
        return bool((logged.get("WhatsApp Sent At") or "").strip())

    def _send_position(c, sheets):
        """Where each lead stands in step 4: queued, or held and why.

        Reported here because the question is asked here. A card saying
        "ready to send" while step 4 does not offer that lead is the shape
        this tool keeps producing -- right code, invisible outcome -- and
        from the outside it is indistinguishable from the confirm button
        having done nothing.
        """
        try:
            queue, excluded = _send_lists(c, sheets)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not work out the send position: %s", exc)
            return {}
        position = {
            m["lead_id"]: {"queued": True, "reason": "", "fixable": False}
            for m in queue
        }
        for row in excluded:
            # A lead cannot be in both, but if step 4 ever offered one it
            # also excluded, being in the queue is the fact that matters.
            reason = row.get("reason", "")
            position.setdefault(row.get("lead_id", ""), {
                "queued": False,
                "reason": _SEND_HOLD_WORDS.get(reason, reason),
                "fixable": reason in _UNBLOCKABLE,
            })
        return position

    def _expected_text(row) -> list[str]:
        """Every string that must appear on the graphic, in reading order.

        One implementation, in `visual`, because the outreach draft reads the
        same thing to write the message from. Two copies drifting apart would
        mean a message describing a picture that says something else.
        """
        from . import visual as visual_lib

        return visual_lib.picture_text(row)

    @app.route("/api/visual/candidates")
    def api_visual_candidates():
        """Who a run would write for, split by how they can be contacted.

        Sixty prompts is not a batch anybody works through: each one is a
        visit to the image tool and a picture to read. `--limit` alone bounds
        the cost but picks whichever leads come first, which is not the same
        as picking the ones worth starting with.

        The split is by the channel a message would actually use, because
        that is the question Lyon asked to work from -- which have WhatsApp
        and which do not -- and it is what decides whether a batch is worth
        an evening.
        """
        from .stages.reachability import is_contactable
        from .stages.visual_brief import GROUP_LABELS, GROUPS, group_of
        from .stages.outreach_draft import needs_an_image
        from .stage import Stage, parse as parse_stage

        c = current()
        empty = {
            "enabled": needs_an_image(c),
            "groups": [
                {"key": k, "label": GROUP_LABELS[k], "count": 0, "leads": [],
                 "contactable": k in ("whatsapp", "email", "messaged"),
                 "second_approach": k == "messaged"}
                for k in GROUPS
            ],
            "total": 0,
        }
        if not empty["enabled"] or not c.secrets.sheet_id:
            return jsonify(empty)

        try:
            sheets = _sheets_client(c)
            leads = sheets.read_rows(schema.LEADS)
            research = sheets.read_rows(schema.RESEARCH)
            briefs = sheets.read_rows(schema.VISUAL_BRIEFS)
            # Who has already had a message. Without this a business
            # contacted months ago with the website pitch counted as fresh
            # work -- which is how 104 "on WhatsApp" read as 104 new
            # conversations.
            outreach = sheets.read_rows(schema.OUTREACH_LOG)
        except Exception as exc:  # noqa: BLE001
            from .sheets import TabMissing

            return jsonify({
                **empty,
                "error": _explain_sheets(exc, c),
                "missing_tab": isinstance(exc, TabMissing),
            }), 200

        buckets: dict[str, list[dict[str, str]]] = {k: [] for k in GROUPS}
        for lead_id, (_n, lead) in leads.items():
            # Exactly what the stage itself would skip, so the counts here and
            # the number the run produces cannot disagree. A count that says
            # one thing and a run that does another makes every run look like
            # it under-delivered -- the reason the research count learned the
            # same rule.
            if lead_id in briefs:
                continue
            current_stage = parse_stage(lead.get("Stage")) or Stage.DISCOVERED
            if current_stage is Stage.DISQUALIFIED:
                continue
            from .stage import rank

            if rank(current_stage) < rank(Stage.RESEARCHED):
                continue
            research_row = (research.get(lead_id) or (0, None))[1]
            outreach_row = (outreach.get(lead_id) or (0, None))[1]
            buckets[group_of(lead, research_row, outreach_row)].append({
                "lead_id": lead_id,
                "name": lead.get("Business Name", "") or lead_id,
                "category": lead.get("Category", ""),
            })

        return jsonify({
            "enabled": True,
            "total": sum(len(v) for v in buckets.values()),
            "groups": [
                {
                    "key": key,
                    "label": GROUP_LABELS[key],
                    "count": len(buckets[key]),
                    # Capped: sixty names is a wall, not a list. The count is
                    # the whole truth; the names are there to recognise who
                    # is in a group before spending on it.
                    "leads": buckets[key][:PREVIEW_CAP],
                    "capped": len(buckets[key]) > PREVIEW_CAP,
                    # Whether this group can be briefed at all. "Not checked
                    # yet" and "no way to contact" are held by the same gate
                    # the site brief uses, so offering a button for them
                    # would promise work that cannot happen.
                    #
                    # "Already messaged" CAN be: a second approach with
                    # something new to say is a real campaign, and it was
                    # asked for by name. It is just never the default.
                    "contactable": key in ("whatsapp", "email", "messaged"),
                    # Said on the card, because picking this group means
                    # contacting a business for the second time.
                    "second_approach": key == "messaged",
                }
                for key in GROUPS
            ],
        })

    @app.route("/api/visual/run", methods=["POST"])
    def api_visual_run():
        """Write prompts for one group, or for an explicit pick.

        Its own endpoint rather than options on /api/run: the group and the
        lead list are validated here, against what the Sheet actually holds,
        and only then turned into arguments. Nothing a browser sends reaches
        an argv unchecked.
        """
        from .stages.visual_brief import GROUPS

        payload = request.json or {}
        groups = payload.get("groups") or []
        lead_ids = payload.get("lead_ids") or []
        if not isinstance(groups, list) or not isinstance(lead_ids, list):
            return jsonify({"error": "groups and lead_ids must be lists"}), 400

        unknown = [g for g in groups if g not in GROUPS]
        if unknown:
            return jsonify({
                "error": f"Unknown group {unknown[0]!r}."
            }), 400

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

        c = current()
        picked: list[str] = []
        if lead_ids:
            # Checked against the Sheet rather than trusted: these become
            # command-line arguments, and a Lead ID is whatever the page
            # chose to send.
            try:
                known = set(_sheets_client(c).read_rows(schema.LEADS))
            except Exception as exc:  # noqa: BLE001
                return jsonify({"error": _explain_sheets(exc, c)}), 200
            picked = [str(x) for x in lead_ids if str(x) in known]
            if not picked:
                return jsonify({
                    "error": "None of those leads are on the Sheet any more. "
                             "Refresh the page and pick again."
                }), 200

        extra: list[str] = []
        for group in groups:
            extra += ["--group", group]
        if picked:
            extra += ["--leads", ",".join(picked)]

        label = "Image prompts"
        if picked:
            label = f"Image prompts ({len(picked)} picked)"
        elif groups:
            label = "Image prompts (" + ", ".join(groups) + ")"

        started = runner.start(
            label, ["visual-brief"], limit, None, {"visual-brief": extra}
        )
        if not started:
            return jsonify({"error": f"{runner.running} is already running"}), 409
        return jsonify({"ok": True, "running": label})

    @app.route("/api/visual/attach", methods=["POST"])
    def api_visual_attach():
        """Take the picture back from the image tool and record it.

        Accepts the bytes (pasted or dropped, as a data URL) or an address to
        download them from -- the image tool offers both, and making Lyon
        save a file first would be a step for no reason.

        Attaching clears any previous confirmation. The words on the new
        picture have not been read, and carrying a tick across from the
        picture it replaced would attach an unchecked graphic to a real
        message, which is the one failure that cannot be undone after
        sending.
        """
        from . import visual as visual_lib

        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400

        c = current()
        try:
            sheets = _sheets_client(c)
            leads = sheets.read_rows(schema.LEADS)
            briefs = sheets.read_rows(schema.VISUAL_BRIEFS)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200

        if lead_id not in briefs:
            return jsonify({
                "error": "That lead has no image prompt yet. Run the "
                         "Infographic step for it first."
            }), 200
        name = (
            briefs[lead_id][1].get("Business Name")
            or leads.get(lead_id, (0, {}))[1].get("Business Name", "")
            or lead_id
        )

        data_url = (payload.get("data_url") or "").strip()
        url = (payload.get("url") or "").strip()
        try:
            if data_url:
                raw, content_type = visual_lib.decode_data_url(data_url)
                source = "upload"
            elif url:
                if not visual_lib.is_fetchable_url(url):
                    return jsonify({
                        "error": "That address cannot be downloaded from here. "
                                 "It has to be a public http(s) link -- or save "
                                 "the picture and paste it in instead."
                    }), 200
                raw, content_type = _download_image(url)
                source = f"url {url}"
            else:
                return jsonify({
                    "error": "Paste the picture, or give the link the image "
                             "tool showed you."
                }), 200
            stored = visual_lib.save_image(name, lead_id, raw, content_type)
        except visual_lib.ImageRejected as exc:
            return jsonify({"error": str(exc)}), 200

        try:
            sheets.update_column(
                schema.VISUAL_BRIEFS, "Image File", {lead_id: stored}
            )
            sheets.update_column(
                schema.VISUAL_BRIEFS, "Image Source", {lead_id: source}
            )
            # Cleared, not carried over. See the docstring.
            sheets.update_column(
                schema.VISUAL_BRIEFS, "Text Confirmed", {lead_id: ""}
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200

        return jsonify({
            "ok": True,
            "lead_id": lead_id,
            "state": "needs_check",
            "bytes": len(raw),
        })

    def _download_image(url: str) -> tuple[bytes, str]:
        """Fetch one picture, refusing anything that is not one.

        Streamed with a cap rather than read whole: an address that answers
        with a hundred megabytes would otherwise be held in memory before
        anything got a chance to refuse it.
        """
        from . import visual as visual_lib

        import requests

        try:
            resp = requests.get(url, timeout=20.0, stream=True)
        except requests.RequestException as exc:
            raise visual_lib.ImageRejected(
                f"Could not download that link: {exc}"
            ) from exc
        if resp.status_code in (401, 403):
            # The common case, and the one a bare status code explains worst.
            # An image tool serves your own generations behind your Google
            # sign-in; this request carries no sign-in at all, so a link that
            # opens perfectly in your browser is refused here.
            raise visual_lib.ImageRejected(
                "That link needs you to be signed in, and this download is "
                "not. Save the picture and paste it in instead -- that always "
                "works, because the file never leaves your computer."
            )
        if resp.status_code == 404:
            raise visual_lib.ImageRejected(
                "That link is gone (404). Image tools expire their links -- "
                "save the picture and paste it in instead."
            )
        if resp.status_code != 200:
            raise visual_lib.ImageRejected(
                f"That link answered {resp.status_code}. Open it in a browser "
                "to check it still works, or paste the picture instead."
            )
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0]
        content_type = content_type.strip().casefold()
        if content_type not in visual_lib.IMAGE_TYPES:
            # Almost always the page's own address rather than the image's.
            # Saying which to copy is the difference between this working and
            # it looking broken -- "that is not an image" is true and useless.
            if content_type.startswith("text/"):
                raise visual_lib.ImageRejected(
                    "That is the address of the page, not of the picture. "
                    "Right-click the picture itself and choose Copy image "
                    "address -- or just save it and paste the file in."
                )
            raise visual_lib.ImageRejected(
                f"That link is {content_type or 'not an image'}, not a "
                "picture. Right-click the picture and choose Copy image "
                "address, or save it and paste the file in."
            )
        chunks, total = [], 0
        for chunk in resp.iter_content(64 * 1024):
            total += len(chunk)
            if total > visual_lib.MAX_IMAGE_BYTES:
                raise visual_lib.ImageRejected(
                    f"That picture is over the "
                    f"{visual_lib.MAX_IMAGE_BYTES // 1_048_576}MB limit."
                )
            chunks.append(chunk)
        return b"".join(chunks), content_type

    @app.route("/api/visual/confirm", methods=["POST"])
    def api_visual_confirm():
        """Record that a person has read the words on the picture.

        The image tool is outside this pipeline, so nothing here can test
        that the text rendered correctly -- and a garbled word on a graphic
        carrying your name is the one failure that cannot be fixed after
        sending. So the check goes where the only eye is, and this is what
        makes it binding: no message attaches a graphic without it.
        """
        from . import visual as visual_lib

        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400

        c = current()
        try:
            sheets = _sheets_client(c)
            briefs = sheets.read_rows(schema.VISUAL_BRIEFS)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200

        entry = briefs.get(lead_id)
        if entry is None:
            return jsonify({"error": "That lead has no image prompt."}), 200
        if visual_lib.resolve_image(entry[1].get("Image File", "")) is None:
            return jsonify({
                "error": "There is no picture to check yet. Paste it in first."
            }), 200

        try:
            sheets.update_column(
                schema.VISUAL_BRIEFS, "Text Confirmed", {lead_id: "yes"}
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200

        # The tick is what lets a message carry this graphic, so this is the
        # moment the message has to be about the graphic. Two ways it is not,
        # and both were reported from real runs:
        #
        #   * the lead was held with no message at all, because the picture
        #     had not come back when drafting ran -- so confirming produced a
        #     "ready" card that step 4 never offered; and
        #   * the lead was drafted as a website lead before the prompt even
        #     existed, so its message offers a page this message is not
        #     about, to a business that may already have read it.
        #
        # Writing it here rather than asking for another press: the confirm
        # IS the decision, and a second button to make the message match the
        # thing just confirmed is a step nobody would know to take.
        wrote = _message_for_the_picture(c, sheets, lead_id)
        return jsonify({
            "ok": True, "lead_id": lead_id, "state": "ready", **wrote,
        })

    def _message_for_the_picture(c, sheets, lead_id) -> dict:
        """Make the stored message one written about this graphic.

        Never fatal. The tick is the gate and it is already saved: losing it
        because a model was slow, or retired, would throw away the one check
        only a person can do and send them round the loop again. A failure is
        reported as a sentence on the card instead, with the message left
        exactly as it was -- which `stale_pitch` in step 4 then flags.
        """
        from .stages.outreach_draft import offers_a_page

        try:
            logged = sheets.read_rows(schema.OUTREACH_LOG).get(lead_id)
            row = (logged or (0, {}))[1]
            message = row.get("WhatsApp Message", "")
            briefs = sheets.read_rows(schema.SITE_BRIEFS).get(lead_id)
            preview_url = (briefs or (0, {}))[1].get("Preview URL", "")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not check %s's message: %s", lead_id, exc)
            return {}

        # Already sent: not ours to rewrite. Step 4 offers "Write a new
        # message for this picture" there, which clears the send status too.
        if _sent_already(row):
            return {}
        stale = not offers_a_page(c, preview_url) and _written_for_a_page(
            message, preview_url
        )
        if message.strip() and not stale:
            return {}

        try:
            from .providers.llm import make_writer
            from .stages import outreach_draft as draft

            draft.regenerate_one(
                c, sheets, make_writer(c, model=c.outreach.model or None), lead_id
            )
        except Exception as exc:  # noqa: BLE001
            return {"message_error": _explain_sheets(exc, c)}
        return {"message_written": True}

    @app.route("/api/visual/clear", methods=["POST"])
    def api_visual_clear():
        """Drop the picture so a corrected one can be pasted in.

        The file itself is left on disk. It cost a generation, the Sheet no
        longer points at it, and deleting someone's only copy of a graphic to
        save a few kilobytes is the wrong trade.
        """
        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400

        c = current()
        try:
            sheets = _sheets_client(c)
            for column in ("Image File", "Image Source", "Text Confirmed"):
                sheets.update_column(
                    schema.VISUAL_BRIEFS, column, {lead_id: ""}
                )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200
        return jsonify({"ok": True, "lead_id": lead_id, "state": "needs_image"})

    @app.route("/api/visual/skip", methods=["POST"])
    def api_visual_skip():
        """Retire one graphic without stopping the lead being messaged.

        Written with ``user_edit=True`` -- Approval Status is a manual
        column, and this is the named exception: an action on a row the user
        has just clicked. A skipped graphic is a decision rather than a wait,
        so the draft stops holding that lead and pitches the page instead.
        """
        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400
        decision = (payload.get("decision") or "Skip").strip()
        if decision not in ("Skip", "Approved"):
            return jsonify({"error": "decision must be Skip or Approved"}), 400

        c = current()
        try:
            sheets = _sheets_client(c)
            sheets.update_column(
                schema.VISUAL_BRIEFS,
                "Approval Status",
                {lead_id: decision},
                user_edit=True,
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200
        return jsonify({"ok": True, "lead_id": lead_id, "approval": decision})

    @app.route("/api/visual/redraft", methods=["POST"])
    def api_visual_redraft():
        """Write a fresh message for a lead that has already been messaged.

        The draft stage decides what is left to do by whether a Lead ID
        appears on the Outreach Log, which is what stops a business being
        written to twice. Correct -- and it meant a business contacted months
        ago with the website pitch could never get a message about its
        graphic. The picture existed, the card said "ready to send", and
        nothing would ever write the words.

        So this is the one deliberate exception, pressed per lead, on a row
        the user is looking at. It does two things: clears the send status so
        the lead returns to the queue, and rewrites the message with the
        picture's own points. Nothing else is touched -- the picture and the
        prompt cost real work, and the old message is what is being replaced,
        not the graphic.
        """
        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400

        c = current()
        from . import visual as visual_lib

        try:
            sheets = _sheets_client(c)
            briefs = sheets.read_rows(schema.VISUAL_BRIEFS)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200

        entry = briefs.get(lead_id)
        if entry is None:
            return jsonify({"error": "That lead has no image prompt."}), 200
        # Refused rather than silently writing a message about a picture
        # nobody has checked: the whole gate exists so a garbled graphic
        # cannot reach a business, and a redraft must not route around it.
        if visual_lib.confirmed_image(entry[1]) is None:
            return jsonify({
                "error": "Check the picture first. A message is written about "
                         "what the graphic says, so it needs a confirmed one."
            }), 200

        # What the row says now, so a failed rewrite can be undone. Clearing
        # the status is what returns a lead to the send queue, and the
        # message is not rewritten until after that -- so a rewrite that
        # fails would leave the *old* message queued with the new picture
        # attached. That is not "visible and fixable", which is what this
        # comment used to claim: it is a website pitch a business has
        # already received, sitting at the top of the queue with a graphic
        # on it, one press from going out a second time. Put it back.
        try:
            logged = sheets.read_rows(schema.OUTREACH_LOG)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200
        before = {
            column: (logged.get(lead_id, (0, {}))[1].get(column) or "")
            for column in ("WhatsApp Status", "WhatsApp Sent At")
        }

        def restore() -> None:
            """Undo the clear. Never allowed to mask the real failure."""
            for column, value in before.items():
                try:
                    sheets.update_column(
                        schema.OUTREACH_LOG, column, {lead_id: value}
                    )
                except Exception:  # noqa: BLE001
                    log.exception("could not restore %s for %s", column, lead_id)

        try:
            from .providers.llm import make_writer
            from .stages import outreach_draft as draft

            for column in before:
                sheets.update_column(schema.OUTREACH_LOG, column, {lead_id: ""})
            out = draft.regenerate_one(
                c, sheets, make_writer(c, model=c.outreach.model or None), lead_id
            )
        except Exception as exc:  # noqa: BLE001
            restore()
            return jsonify({"error": _explain_sheets(exc, c)}), 200

        return jsonify({"ok": True, "lead_id": lead_id, **out})

    @app.route("/api/visual/regenerate", methods=["POST"])
    def api_visual_regenerate():
        """Re-roll one lead's prompt, rotating off the look it already had.

        The targeted answer to "the style list changed, so every prompt
        written before it is the old one". The picture and its confirmation
        survive: they cost a generation and a person's attention, and the new
        prompt has not replaced them yet.
        """
        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400

        c = current()
        try:
            from .providers.llm import make_writer
            from .stages import visual_brief as visual_brief_stage

            out = visual_brief_stage.regenerate_one(
                c, _sheets_client(c), make_writer(c), lead_id
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _explain_sheets(exc, c)}), 200
        return jsonify({"ok": True, **out})

    @app.route("/api/track", methods=["GET", "POST"])
    def api_track():
        """Read or set which artefact this campaign's message carries.

        One setting rather than two that can disagree: the track decides
        whether a graphic is written, whether the draft waits for one, and
        what the message claims. The signature is saved with it because a
        campaign asking for graphics with nothing to sign them refuses to
        load -- saving the track alone would leave a tool that will not start.
        """
        from .campaign_edit import CampaignEditError, rewrite_track
        from .config import TRACKS

        if request.method == "GET":
            c = current()
            return jsonify({
                "track": c.outreach.track,
                "tracks": sorted(TRACKS),
                "signature": c.visual.signature,
                "tool_name": c.visual.image_tool_name,
                "tool_url": c.visual.image_tool_url,
                # Offered to the field, never saved on his behalf. Nothing in
                # this repo can reach that host to check it -- the same reason
                # build.image_model discovers rather than guesses -- so the
                # value is in front of him before he presses Save, and the
                # button only exists once he has.
                "tool_url_suggestion": SUGGESTED_IMAGE_TOOL_URL,
            })

        payload = request.json or {}
        track = (payload.get("track") or "").strip().lower()
        if track not in TRACKS:
            return jsonify({
                "error": f"Unknown track {track!r}. Expected one of {sorted(TRACKS)}."
            }), 400
        signature = (payload.get("signature") or "").strip()
        # Absent means "leave it alone"; present and empty means "no button".
        tool_name = payload.get("tool_name")
        tool_url = payload.get("tool_url")
        if tool_name is not None:
            tool_name = str(tool_name).strip()
        if tool_url is not None:
            tool_url = str(tool_url).strip()

        try:
            path = _writable_campaign()
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        try:
            original = path.read_text()
            path.write_text(
                rewrite_track(original, track, signature, tool_name, tool_url)
            )
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        except OSError as exc:
            return jsonify({"error": f"Could not save {path}: {exc}"}), 200

        try:
            state["config"] = load_config(path)
        except ConfigError as exc:
            path.write_text(original)
            return jsonify({"error": f"Reverted -- {exc}"}), 200

        c = current()
        return jsonify({
            "ok": True,
            "track": track,
            "signature": signature,
            "tool_name": c.visual.image_tool_name,
            "tool_url": c.visual.image_tool_url,
        })

    @app.route("/api/visual/rotation", methods=["POST"])
    def api_visual_rotation():
        """Narrow the series to the styles and layouts that actually work.

        The one place this is cut. Both lists feed every industry's rotation,
        so unticking a style here removes it everywhere -- which is the point:
        the alternative is seven industry rings to keep in step by hand.
        """
        from .campaign_edit import CampaignEditError, rewrite_visual_rotation

        payload = request.json or {}
        styles = payload.get("styles") or []
        layouts = payload.get("layouts") or []
        if not isinstance(styles, list) or not isinstance(layouts, list):
            return jsonify({"error": "styles and layouts must be lists"}), 400

        try:
            path = _writable_campaign()
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        try:
            original = path.read_text()
            path.write_text(rewrite_visual_rotation(original, styles, layouts))
        except CampaignEditError as exc:
            return jsonify({"error": str(exc)}), 200
        except OSError as exc:
            return jsonify({"error": f"Could not save {path}: {exc}"}), 200

        try:
            state["config"] = load_config(path)
        except ConfigError as exc:
            path.write_text(original)
            return jsonify({"error": f"Reverted -- {exc}"}), 200

        c = current()
        return jsonify({
            "ok": True,
            "styles": list(c.visual.styles),
            "layouts": list(c.visual.layouts),
        })

    def _send_lists(c, sheets):
        """Step 4's two lists: what is waiting to send, and what is not.

        A function rather than only a route because the infographic step has
        to answer "is this lead in step 4, and if not why" -- and the one
        thing worse than not answering is answering differently. A second
        implementation of these rules would drift, and a page saying a lead
        is ready beside a queue that does not offer it is unexplainable from
        the outside.
        """
        from . import phone as phone_lib
        from .providers.whatsapp import WA_SENT_STATUSES, chat_link as wa_chat_link
        from .stages.reachability import _country_of, can_whatsapp

        outreach = sheets.read_rows(schema.OUTREACH_LOG)
        leads = sheets.read_rows(schema.LEADS)
        # For the cached WhatsApp verdict. Worth the extra read: without
        # it a US number falls back to the numbering-plan guess, which
        # cannot tell mobile from landline there and hides the lead.
        research = sheets.read_rows(schema.RESEARCH)

        # Read once for the whole queue rather than per lead. Skipped
        # entirely on the original track.
        visuals = _visual_rows(c, sheets)
        pages = _pages_offered(c, sheets)

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
                    # Whether a picture will be attached, and which one.
                    # Shown in the queue because until now the only way to
                    # find out was to send the message and look at your phone
                    # -- and on the infographic track the picture IS the
                    # pitch, so sending without it is worse than a plain
                    # message: the words describe something that is not there.
                    "has_shot": _shot_for(
                        c, lead, lead_id,
                        (visuals.get(lead_id) or (0, None))[1],
                    ) is not None,
                    "shot_kind": (
                        "visual"
                        if _visual_confirmed(visuals.get(lead_id))
                        else "site"
                    ),
                    # Written before this campaign moved to graphics, and
                    # still offering the page. Flagged rather than hidden:
                    # the message is true, it is just the wrong pitch, and
                    # dropping it from the queue would look like the lead
                    # had vanished.
                    "stale_pitch": _written_for_a_page(
                        row.get("WhatsApp Message", ""), pages.get(lead_id, "")
                    ),
                    # The relay-free way to send this. Built here because
                    # this is where the number has just been normalised with
                    # the lead's own country; the browser must never work a
                    # dialling code out for itself.
                    "wa_link": wa_chat_link(
                        phone_lib.normalise(
                            phone, _country_of(lead, c.compliance.country_code)
                        ),
                        row.get("WhatsApp Message", ""),
                    ),
                }
            )

        return queue, excluded

    @app.route("/api/whatsapp/queue")
    def api_whatsapp_queue():
        """Leads waiting for WhatsApp send, with phone + message text."""
        c = current()
        if not c.secrets.sheet_id:
            return jsonify({"queue": [], "excluded": []})
        try:
            queue, excluded = _send_lists(c, _sheets_client(c))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({"queue": queue, "excluded": excluded})

    @app.route("/api/whatsapp/mark-sent", methods=["POST"])
    def api_whatsapp_mark_sent():
        """Record that you sent this one yourself, through WhatsApp.

        The relay knows when it sent something. Nothing knows when *you* did
        -- following a click-to-chat link opens WhatsApp and tells this
        dashboard nothing at all, so the lead would sit at the top of the
        queue forever, inviting the same business to be messaged twice.

        So this is a claim rather than an observation, and it is recorded as
        one: the status is `unconfirmed`, the same word used when WhatsApp
        accepted a message without acknowledging delivery. **Send again**
        already exists for the case where the claim turns out to be wrong.
        """
        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400

        from .models import utc_now

        try:
            sheets = _sheets_client(current())
            # Both cells, or the queue keeps offering it: the status is what
            # excludes it and the timestamp is what the daily cap counts.
            written = sheets.update_column(
                schema.OUTREACH_LOG, "WhatsApp Status", {lead_id: "unconfirmed"},
                user_edit=True,
            )
            sheets.update_column(
                schema.OUTREACH_LOG, "WhatsApp Sent At", {lead_id: utc_now()},
                user_edit=True,
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        if not written:
            return jsonify({"error": f"No Outreach Log row for {lead_id}"}), 200
        return jsonify({"ok": True, "status": "unconfirmed"})

    @app.route("/api/whatsapp/undo-send", methods=["POST"])
    def api_whatsapp_undo_send():
        """Put one already-sent lead back in the send queue.

        A message can be reported as sent and still not arrive -- the
        "Waiting for this message" messages were exactly that: acknowledged
        by WhatsApp, unreadable on the phone. The Sheet is then wrong, and
        the only way to correct it was to open the Sheet and clear a cell by
        hand, which is precisely what the dashboard exists to avoid.

        Only what is blocking the resend is written. The message, the
        screenshot and the draft are untouched, so pressing Send afterwards
        sends the same thing again rather than a regenerated one.
        """
        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400

        c = current()
        try:
            sheets = _sheets_client(c)
            rows = sheets.read_rows(schema.OUTREACH_LOG)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200

        found = rows.get(lead_id)
        if found is None:
            return jsonify({
                "error": "No drafted message for this lead, so there is "
                         "nothing to send again."
            }), 200
        row = found[1]

        cleared = []
        try:
            # The two the dashboard wrote when it sent. Clearing the status
            # is what returns the lead to the queue; clearing the timestamp
            # keeps the row honest, since it is no longer true that this
            # went out at that moment.
            if (row.get("WhatsApp Status") or "").strip():
                sheets.update_column(
                    schema.OUTREACH_LOG, "WhatsApp Status", {lead_id: ""}
                )
                cleared.append("WhatsApp Status")
            if (row.get("WhatsApp Sent At") or "").strip():
                sheets.update_column(
                    schema.OUTREACH_LOG, "WhatsApp Sent At", {lead_id: ""}
                )
                cleared.append("WhatsApp Sent At")

            # Status is a manual column, so a stage may never write it. This
            # is the exception the rule names: a dashboard action on one
            # named row the user has just clicked. Only written when it is
            # actually what is holding the lead out of the queue -- a row
            # already reading as drafted is left alone.
            #
            # Asked of `_is_drafted_outreach` rather than compared against
            # the word, because a blank Status counts as drafted only when
            # the row carries the draft's own markers. Testing the string
            # left that case unrepairable: excluded from the queue, and
            # answered with "nothing was blocking this one".
            status = (row.get("Status") or "").strip()
            if not _is_drafted_outreach(row):
                sheets.update_column(
                    schema.OUTREACH_LOG,
                    "Status",
                    {lead_id: "Drafted"},
                    user_edit=True,
                )
                cleared.append(
                    f"Status ({status} -> Drafted)" if status
                    else "Status (blank -> Drafted)"
                )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200

        if not cleared:
            return jsonify({
                "error": "Nothing was blocking this one -- it should already "
                         "be in the send list."
            }), 200
        return jsonify({"ok": True, "cleared": cleared})

    @app.route("/api/whatsapp/skip", methods=["POST"])
    def api_whatsapp_skip():
        """Take one message out of the send list without sending it.

        Not a disqualification: the lead keeps its research, its site and its
        drafted message, and the row moves down to the excluded list where
        **Send again** puts it straight back. It is the answer to "not this
        one, not now", which previously meant either sending it anyway or
        editing the Sheet by hand.
        """
        payload = request.json or {}
        lead_id = (payload.get("lead_id") or "").strip()
        if not lead_id:
            return jsonify({"error": "lead_id is required"}), 400

        c = current()
        try:
            sheets = _sheets_client(c)
            rows = sheets.read_rows(schema.OUTREACH_LOG)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200

        if lead_id not in rows:
            return jsonify({
                "error": "No drafted message for this lead, so there is "
                         "nothing to skip."
            }), 200

        try:
            # Status is manual, and this is the exception the rule names: one
            # named row the user has just clicked. "Skipped" is not a stage,
            # only a note in the Status cell -- what actually removes the row
            # from the queue is that it no longer reads "Drafted".
            sheets.update_column(
                schema.OUTREACH_LOG, "Status", {lead_id: "Skipped"}, user_edit=True
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
        return jsonify({"ok": True})

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

        # The cap in the campaign config was read and never applied -- the
        # relay enforces only its own MAX_PER_DAY, so the number Lyon set in
        # his campaign YAML did nothing. Count what today has already sent
        # and refuse past it. Timestamps are ISO, so the date is the first
        # ten characters.
        from .models import utc_now

        cap = c.outreach.whatsapp_max_per_day
        if cap > 0:
            today = utc_now()[:10]
            try:
                # A set comprehension over row dicts once raised
                # "unhashable type" on every call and was swallowed into
                # sent_today = 0, so the cap never tripped. Count in one
                # pass instead.
                sent_today = sum(
                    1 for row in _sheets_client(c).read_rows(schema.OUTREACH_LOG).values()
                    if (row[1].get("WhatsApp Sent At") or "")[:10] == today
                )
            except Exception:  # noqa: BLE001 -- never let bookkeeping stop a send
                sent_today = 0
            if sent_today >= cap:
                return jsonify(
                    {
                        "error": f"Daily cap reached: {sent_today} of {cap} WhatsApp "
                        "messages already sent today. Wait until tomorrow, or "
                        "raise outreach.max_per_day in your campaign."
                    }
                ), 429

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
            shot = (
                _shot_for(c, lead_row, lead_id, _visual_row_for(c, lead_id))
                if lead_row is not None
                else None
            )

            result = client.send_message(phone, message, send_country, shot)
            # Carried through so the page can say "sent, not yet confirmed"
            # rather than either lying or failing.
            note = result.note
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
                "note": note,
                "timestamp": result.timestamp,
            }
        )

    return app


# The only address that needs no password: nothing but this machine can
# reach it. Every other value puts the dashboard on a network.
LOOPBACK = "127.0.0.1"

# A public address needs a password worth having. Twelve characters is not a
# strong claim on its own -- it is the point below which a published
# dashboard is obviously indefensible, which is the only line code can draw.
MIN_TUNNEL_PASSWORD = 12
# Every interface this machine has, which is what a phone on the same wifi
# needs in order to reach it.
ALL_INTERFACES = "0.0.0.0"


class UnsafeBinding(Exception):
    """Refusing to put an unauthenticated dashboard on a network."""


def _routed_address() -> str:
    """The address the routing table would use to reach the internet.

    A good guess and nothing more. It is the wrong answer whenever something
    sits in front of the real network card -- a VPN returns its tunnel
    address, and WSL, Docker Desktop and Hyper-V each add an adapter that can
    win this race. The phone cannot reach any of those, and the failure looks
    exactly like a firewall: a page that never loads.
    """
    import socket

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Sends nothing. It only asks which local address would be used.
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return ""


def _private(address: str) -> bool:
    """Is this an address a phone on the same home network could reach?"""
    import ipaddress

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(
        parsed.version == 4
        and parsed.is_private
        and not parsed.is_loopback
        and not parsed.is_link_local  # 169.254.x = DHCP did not answer
    )


def likely_virtual(address: str) -> bool:
    """Ranges that are usually a virtual adapter rather than the real network.

    Not a verdict -- plenty of real networks use 172.16/12, and naming one
    wrongly is cheap because every candidate is offered anyway. It only
    decides what order they are printed in, so the address most likely to
    work is read first.
    """
    return address.startswith("172.") or address.startswith("192.168.56.")


def lan_addresses() -> list[str]:
    """Every address on this machine a phone might be able to reach.

    One guess was not enough. The routed address is the wrong one on any
    machine carrying a VPN or a virtual adapter, and the person reading it
    has no way to tell -- so all of them are offered, most likely first, and
    trying two is faster than explaining `ipconfig` over a message.
    """
    import socket

    found: list[str] = []

    def add(address: str) -> None:
        if _private(address) and address not in found:
            found.append(address)

    add(_routed_address())
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass

    # Real network cards before virtual ones, and otherwise the order they
    # were discovered -- the routed guess stays first among equals because it
    # is right on an ordinary machine.
    return sorted(found, key=likely_virtual)


def lan_address() -> str:
    """The single best guess, or "" if there is none."""
    addresses = lan_addresses()
    return addresses[0] if addresses else ""


def serve(
    config_path: str | None = None,
    *,
    port: int = 8765,
    open_browser: bool = True,
    host: str | None = None,
    phone: bool = False,
    tunnel: bool = False,
):
    """Run the dashboard.

    ``phone=True`` means "serve to the network as well, if that is safe" --
    it is not a promise to do so. Whether it is safe depends on a password
    being set, and that answer lives here, where the config has already been
    read. It used to live in Dashboard.bat as a findstr against .env, which
    is a regex I could not test on Windows from here and which silently did
    nothing when it was wrong: no flag, no phone access, and no clue why.
    One decision, in the one place that has the information.
    """
    config = load_config(config_path)
    has_password = bool(config.secrets.dashboard_password)

    if host is None:
        # A container's loopback is reachable by nothing at all -- not the
        # platform's router, not its health check, not you. So a hosted
        # deployment binds every interface without being asked, and the
        # password gate below is what keeps that safe. The launcher has no
        # flag to forget, which is the same reason --phone became a request
        # rather than an address.
        wants_network = phone or paths.is_hosted()
        host = ALL_INTERFACES if (wants_network and has_password) else LOOPBACK

    # Nothing here can open a browser, and trying starts a thread that fails
    # a second later with a message about a missing display -- in the logs of
    # a server nobody is reading, next to the address that actually works.
    if paths.is_hosted():
        open_browser = False

    # The one hard rule. The dashboard spends money, messages real
    # businesses and reads the Sheet, and it has no other protection -- on a
    # cafe or hotel network, everyone in the building is on that network.
    # Binding wide open without a password is not a thing to warn about; it
    # is a thing to refuse.
    if paths.is_hosted() and not has_password:
        # Said separately because the fix differs: there is no .env to edit
        # by hand on a platform that injects its own environment.
        raise UnsafeBinding(
            "Refusing to start with no password set.\n\n"
            "This deployment is reachable from the internet, and the "
            "dashboard spends money, messages real businesses and reads "
            "your Sheet.\n\n"
            "Set DASHBOARD_PASSWORD in the host's environment "
            f"({MIN_TUNNEL_PASSWORD} characters or more) and redeploy."
        )

    if host != LOOPBACK and not has_password:
        raise UnsafeBinding(
            f"Refusing to serve on {host} with no password set.\n\n"
            "Anyone on the same network could open this dashboard, spend "
            "your API credits and message businesses as you.\n\n"
            "Set one first:\n"
            "    DASHBOARD_PASSWORD=something-only-you-know\n"
            "in your .env file, then start it again."
        )

    # A tunnel is a different order of exposure from a home network, and the
    # password stops being the last line of defence and becomes the only one.
    # A short password on a wifi nobody else is on is a judgement call; the
    # same password published to the internet, where scanners find new
    # hostnames within minutes, is not.
    if tunnel:
        if not has_password:
            raise UnsafeBinding(
                "Refusing to publish this dashboard to the internet with no "
                "password set.\n\n"
                "Set one first:\n"
                "    DASHBOARD_PASSWORD=something-only-you-know\n"
                "in your .env file, then start it again."
            )
        if len(config.secrets.dashboard_password) < MIN_TUNNEL_PASSWORD:
            raise UnsafeBinding(
                f"Refusing to publish this dashboard to the internet with a "
                f"password shorter than {MIN_TUNNEL_PASSWORD} characters.\n\n"
                "On your own wifi a short one is a judgement call. On a public "
                "address it is the only thing between a stranger and a tool "
                "that spends money and messages real businesses -- and new "
                "addresses get scanned within minutes of appearing.\n\n"
                "Make DASHBOARD_PASSWORD longer in your .env file, then start "
                "it again."
            )

    app = create_app(config)
    url = f"http://{LOOPBACK}:{port}"

    opened = None
    if tunnel:
        from .providers.tunnel import Tunnel, TunnelError

        # Opened before the browser and before app.run, so a failure is an
        # error on the way in rather than a dashboard that started, said
        # nothing about the tunnel, and left someone waiting on a phone.
        opened = Tunnel(port)
        try:
            if opened.binary is None:
                # Installing a program is the step that stalls someone who
                # is not a programmer, and it is the only one left in "put
                # the dashboard on my phone". Say what is being fetched and
                # from where, then do it.
                print("\n  cloudflared is not here yet. Downloading Cloudflare's")
                print("  own build (about 40MB, once only)...")
                where = opened.ensure_binary()
                print(f"  Saved to {where}")
            print("\n  Opening a public address (this takes a few seconds)...")
            public = opened.start()
        except TunnelError as exc:
            raise UnsafeBinding(f"{exc}") from exc

        # Two things depend on knowing this, and neither may be guessed:
        # which address a login attempt counts against, and whether the
        # session cookie may only travel over https.
        app.config["state"]["tunnelled"] = True
        app.config.update(SESSION_COOKIE_SECURE=True)

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"\n  Dashboard running at {url}")
    if opened is not None:
        print("\n  On any phone, anywhere -- no wifi, no firewall, no IP address:")
        print(f"      {public}/app")
        print("\n  This address is PUBLIC. Your password is the only thing")
        print("  in front of it. It changes every time you start the")
        print("  dashboard, so remake the home-screen icon when it does.")
    if host == LOOPBACK:
        if phone and not has_password:
            # Asked for, and not given. Saying nothing here is how the last
            # attempt failed silently.
            print("\n  Phone access is OFF: no password is set.")
            print("  Add this line to your .env file and start again:")
            print("      DASHBOARD_PASSWORD=something-only-you-know")
    else:
        candidates = lan_addresses()
        host_part = candidates[0] if candidates else "<this-pc-ip>"
        print("\n  On your phone, on the same wifi, open:")
        print(f"      http://{host_part}:{port}/app")
        # A machine with a VPN or a virtual adapter has several addresses and
        # only one of them is the network the phone is on. Naming just the
        # first sends people to an address that can never answer, which looks
        # identical to a firewall problem.
        for extra in candidates[1:]:
            print(f"      or  http://{extra}:{port}/app")
        if len(candidates) > 1:
            print("      (more than one network card -- try them in order)")
        if not candidates:
            print("      (run  ipconfig  and use the IPv4 Address)")
        print("  Type http:// at the front -- without it phones try https,")
        print("  and show a security warning for a page that is fine.")
        print("  It asks for your dashboard password, then shows the new")
        print("  dashboard. Share -> Add to Home Screen makes it an app.")
        print(f"\n  The older page is at  http://{host_part}:{port}/")
        print("\n  If Windows asks about the firewall, tick Private networks")
        print("  and allow it -- the phone cannot connect otherwise.")
    # The morning batch, if one was asked for. Started before app.run so a
    # bad time is an error on the way in rather than a morning where nothing
    # happened and nothing said why.
    wanted = os.getenv(scheduler.ENV_VAR, "").strip()
    if wanted:
        from datetime import datetime

        try:
            at = scheduler.parse_time(wanted)
        except scheduler.ScheduleError as exc:
            raise UnsafeBinding(str(exc)) from exc
        scheduler.start(
            at,
            # Exactly what the Full pipeline button does: discovery through
            # drafting, stopping before anything is built or sent. The
            # Runner refuses a second run while one is going, and returning
            # that refusal is what stops the scheduler stacking one on top
            # of whatever a person just started.
            lambda: app.config["runner"].start(
                "Daily run", full_run_for(current()), None, None
            ),
        )
        print(f"\n  {scheduler.describe(at, datetime.now().astimezone())}")
    else:
        print(f"\n  No daily run scheduled. Set {scheduler.ENV_VAR}=07:30 for one.")

    print("\n  Close this window when you're done.\n")
    try:
        app.run(host=host, port=port, debug=False)
    finally:
        # A tunnel still running after the dashboard stopped would leave the
        # machine published with nobody aware of it.
        if opened is not None:
            opened.stop()


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
<!-- viewport-fit=cover lets the page paint under the Dynamic Island and the
     home indicator; the safe-area padding below keeps content out of both. -->
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>SME Outreach</title>
<!-- Added to the Home Screen this opens without Safari's chrome, which is
     what makes it feel like an app rather than a bookmark. No store, no
     build step -- the same single page, installed. -->
<link rel="manifest" href="/manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Outreach">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#f6f5f2" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#151311" media="(prefers-color-scheme: dark)">
<link rel="apple-touch-icon" href="/icon.png">
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
  /* An error that explains itself arrives with line breaks in it, and a
     <span> throws every one of them away -- the three numbered steps that
     fix an expired Google sign-in ran together into one paragraph on the
     only screen he reads them from. The guidance was written and then
     flattened, which is the same as not writing it. */
  .long-err { color:var(--accent); white-space:pre-wrap; font-size:13px;
              line-height:1.55; }
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
  /* Sits at the far right of the status row: reachable when wanted, never
     next to the thumb that is pressing Send. */
  .wa-status .wa-reset { margin-left:auto; font-size:12.5px; padding:5px 10px;
                         color:var(--muted); }
  .wa-status .wa-reset:hover:not(:disabled) { color:var(--accent);
                                              border-color:var(--accent); }
  .wa-reset-note { margin:-6px 0 12px; font-size:13px; color:var(--muted); }
  /* Small, and inside the cell it acts on, so there is never a question of
     which business a press applies to. */
  .wa-undo { margin-top:6px; font-size:11.5px; padding:3px 8px; width:100%; }
  /* Quieter than Send, and never beside it without a gap: this sits next to
     the button that spends the message. */
  .wa-skip { color:var(--muted); margin-right:6px; }
  .wa-skip:hover:not(:disabled) { color:var(--accent); border-color:var(--accent); }
  .reach-drop { font-size:11.5px; padding:2px 8px; color:var(--muted); }
  .reach-drop:hover:not(:disabled) { color:var(--accent); border-color:var(--accent); }
  .reach-note { font-size:12.5px; color:var(--muted); padding:2px 0 2px 10px; }
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
  /* The infographic panel. The thumbnail is deliberately the size WhatsApp
     shows in a chat: a graphic that reads at full width and not at this size
     is a graphic nobody will read. */
  /* Collapsed by default: sixty cards is not a list anybody reads, and the
     header carries everything needed to decide whether to open one. */
  /* The proofreading box is visually apart from the message below it: the
     two were read as one thing, and only one of them is ever sent. */
  .viz-proof { margin-top:10px; padding:10px 12px; border-radius:6px;
               border:1px solid #3a3326; background:#1b1811; }
  .viz-next { margin-top:12px; padding:10px 12px; border-radius:6px;
              border:1px solid #274034; background:#101c17; }
  .viz-sent { margin-top:12px; padding:10px 12px; border-radius:6px;
              border:1px solid #2a3a33; background:#141a18; }
  .viz-head { display:flex; align-items:center; gap:10px; cursor:pointer;
              user-select:none; }
  .viz-caret { color:#49C990; width:1em; }
  .viz-row { padding:10px 14px; }
  .viz-group { margin:6px 0; }
  .viz-group > summary { cursor:pointer; }
  .viz-picks { display:flex; flex-wrap:wrap; gap:4px 14px; margin:8px 0 0 14px; }
  .viz-pick { display:flex; align-items:center; gap:6px; }
  button.ghost { background:transparent; border:1px solid #2a3a33; color:#8fa39a; }
  .viz-shot { display:block; width:260px; max-width:100%; border-radius:6px;
              border:1px solid #2a3a33; }
  .viz-small { display:block; width:120px; max-width:40%; border-radius:4px;
               border:1px solid #2a3a33; }
  .viz-words { margin:8px 0 0; padding:8px 10px; border-radius:6px;
               background:#11201a; border:1px solid #274034;
               white-space:pre-wrap; font-family:ui-monospace,Consolas,monospace;
               font-size:13px; line-height:1.5; }
  .viz-drop { border:1px dashed #3c5a4c; border-radius:6px; padding:12px;
              text-align:center; cursor:pointer; }
  .viz-drop:focus-visible { outline:2px solid #49C990; outline-offset:2px; }
  .viz-generic { color:#D6AA66; }
  .wa-shot { display:flex; align-items:center; gap:8px; margin:6px 0;
             text-decoration:none; color:var(--muted); font-size:12px; }
  .wa-shot img { width:52px; height:70px; object-fit:cover; object-position:top;
                 border:1px solid var(--line); border-radius:4px; }
  .wa-msg-card .wa-state.ok { color:var(--ok); }
  .wa-msg-card .wa-state.err { color:var(--accent); }
  button.wa-send { background:var(--ok); border-color:var(--ok); color:#fff; }
  button.wa-send:hover:not(:disabled) { opacity:.9; color:#fff; }
  button.wa-send.confirm { background:var(--accent); border-color:var(--accent); }
  .wa-actions { display:flex; gap:8px; flex-wrap:wrap; }
  /* An anchor, not a button: it has to open WhatsApp, and a button that
     navigates is a button that a long-press cannot copy or open elsewhere.
     Styled as the primary action, because on a hosted deployment it is the
     only one there is. */
  a.wa-hand { display:inline-flex; align-items:center; justify-content:center;
              background:var(--ok); border:1px solid var(--ok); color:#fff;
              border-radius:6px; padding:7px 12px; font-size:13px;
              text-decoration:none; cursor:pointer; }
  a.wa-hand:hover { opacity:.9; color:#fff; }
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

  /* ---- phone ----
     Everything below is for using this from a phone on the same wifi. The
     page was built for a desktop and reflows well enough, but three things
     do not survive the move without saying so. */
  @media (max-width:640px) {
    /* Under the Dynamic Island and around the home indicator. viewport-fit
       is cover, so without this the first heading sits under the clock. */
    .wrap { padding:calc(16px + env(safe-area-inset-top))
                    calc(14px + env(safe-area-inset-right))
                    calc(40px + env(safe-area-inset-bottom))
                    calc(14px + env(safe-area-inset-left)); }

    /* 16px is the threshold below which Safari zooms the whole page in on
       focus -- and it does not zoom back out. Tapping the message box to fix
       a typo left the page magnified with no obvious way back. */
    input, textarea, select, .wa-msg-card .wa-edit { font-size:16px; }

    /* A finger is not a mouse pointer. 44px is Apple's own minimum and these
       are buttons that send messages to real businesses. */
    button { min-height:44px; padding:11px 16px; }
    .wa-actions { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .wa-actions button, .wa-actions a.wa-hand,
    .wa-msg-card .wa-row button { width:100%; }
    .wa-msg-card .wa-row { flex-wrap:wrap; gap:8px; }

    /* One column: two 160px cards side by side on a phone are unreadable,
       and the excluded grid was five across. */
    .wa-excl-grid { grid-template-columns:1fr; }
    .steps, .funnel { gap:6px; }
    .q input { flex:1 1 100%; }
    .sendrow { flex-wrap:wrap; gap:6px; }
    h2 { font-size:19px; margin:32px 0 10px; }
    /* Wide content scrolls inside itself rather than pushing the page sideways.
       A horizontally scrolling page on a phone feels broken. */
    table { display:block; overflow-x:auto; }
  }
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
    <div class="row" style="gap:14px">
      <a href="/app" style="display:inline-block;padding:7px 14px;border-radius:8px;
         background:var(--accent,#b4472e);color:#fff;font-weight:600;
         font-size:14px;text-decoration:none">New dashboard &rsaquo;</a>
      <a class="sub" href="/guide" target="_blank" rel="noopener"><b>Handbook &rsaquo;</b></a>
    </div>
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
        <button data-run="publish">Publish to Cloudflare</button>
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
    <label class="sub" style="display:flex;align-items:center;gap:7px;margin-left:auto">
      <input type="checkbox" id="design-showcase" onchange="setDesignLevel(this.checked)"
             style="width:auto">
      Full art direction
    </label>
    <span class="hint" id="design-note"></span>
  </div>
  <p class="hint" id="design-help">Adds the craft rules to every site prompt
    &mdash; type as a scale, layout that breaks the centred column, colour
    ratio, motion given in real durations. It changes only the prompt you
    paste, never the research, so it costs nothing per lead. Turn it off if
    the pages come back worse.</p>
  <p class="hint">Writes each approved brief to a file in your
    <code>sites\\</code> folder using Gemini. Nothing is published and nothing
    is sent &mdash; the run prints the path of every page so you can open one
    and look at it first. A page that comes back broken is refused and never
    written. Gemini's free tier allows 10 pages a minute, so 60 takes about
    seven minutes, mostly waiting. Pages already written are skipped and
    cost nothing.</p>
  <div id="queue"></div>

  <details class="card" style="margin-bottom:14px">
    <summary class="sub"><b>Other services listed at the end of every message</b></summary>
    <p class="hint" style="margin:8px 0">Appended word for word to every
      message, email and WhatsApp alike &mdash; the model never rewrites these.
      One service per line. Leave the list empty and messages end as they did
      before.</p>
    <label class="sub">The line that introduces them
      <input id="svc-intro" style="width:100%;margin:4px 0 10px"></label>
    <label class="sub">One service per line
      <textarea id="svc-list" rows="4" style="height:auto"></textarea></label>
    <div class="row" style="margin-top:8px">
      <button onclick="saveServices()">Save services</button>
      <span class="hint" id="svc-note"></span>
    </div>
  </details>

  <details class="card" style="margin-bottom:14px" id="track-settings">
    <summary class="sub"><b>What the message pitches</b></summary>
    <p class="hint" style="margin:8px 0">One setting, not three that can
      disagree. It decides what gets written, what the send step waits for,
      and what the message claims.</p>
    <label class="sub" style="display:block;margin-bottom:8px">
      <select id="track-pick" onchange="trackPicked()" style="width:100%;margin-top:4px">
        <option value="website">Website &mdash; build a demo page and link to it</option>
        <option value="visual">Infographic &mdash; a picture naming the gaps, no site</option>
        <option value="both">Both &mdash; the picture attached and the link beside it, in one message</option>
      </select>
    </label>
    <label class="sub" id="sig-wrap" style="display:none">The ribbon on every
      graphic, word for word
      <input id="track-sig" placeholder="by Lyon, Business Growth Consultant"
             style="width:100%;margin:4px 0 0">
    </label>
    <p class="hint" id="sig-help" style="display:none;margin:6px 0 0">Required
      for a graphic. It is the one thing that makes the series read as
      yours, so the tool will not start without it.</p>
    <div id="tool-wrap" style="display:none;margin-top:10px">
      <label class="sub" style="display:block">Where you make the pictures
        <input id="tool-name" style="width:100%;margin:4px 0 8px">
      </label>
      <label class="sub" style="display:block">Its web address
        <input id="tool-url" placeholder="https://..." style="width:100%;margin:4px 0 0">
      </label>
      <p class="hint" style="margin:6px 0 0">This becomes the
        <b>Open</b> button on every card in step 3b. The address below is a
        suggestion, not something checked &mdash; nothing in this tool can
        reach that site to confirm it. If it is not where the tool opens for
        you, copy the address out of your browser and paste it here. Clear
        the box and the button simply does not appear.</p>
    </div>
    <div class="row" style="margin-top:10px">
      <button onclick="saveTrack()">Save</button>
      <span class="hint" id="track-note"></span>
    </div>
  </details>

  <h2 id="visual-step"><span class="stepno">3b</span> Infographic
      <span class="sub" style="text-transform:none;letter-spacing:0">
      &mdash; the picture that pitches the gaps. Copy the prompt, make the
      picture, paste it back</span>
      <a class="sub" href="/guide#the-second-pitch-a-picture-instead-of-a-page"
         target="_blank" rel="noopener">What is this?</a></h2>
  <div class="row" style="margin-bottom:10px" id="visual-controls">
    <span class="hint" id="visual-note"></span>
  </div>
  <div class="card" id="visual-pick" style="margin-bottom:14px">
    <div class="row" style="align-items:center;gap:10px;flex-wrap:wrap">
      <b class="sub">Who to write for</b>
      <label class="sub" style="display:flex;align-items:center;gap:6px;margin-left:auto">
        How many
        <input id="viz-limit" type="number" min="1" max="500" value="10"
               style="width:5.5em">
      </label>
    </div>
    <p class="hint" style="margin:8px 0">Each prompt is a visit to the image
      tool and a picture to read, so a batch of sixty is a week, not a
      morning. Pick the group worth starting with &mdash; or tick the exact
      businesses &mdash; and it writes that many and stops.</p>
    <div id="visual-groups"></div>
  </div>
  <details class="card" style="margin-bottom:14px" id="rotation-settings">
    <summary class="sub"><b>Which looks are in the rotation</b></summary>
    <p class="hint" style="margin:8px 0">Each industry rotates through its own
      pairs and never repeats the previous lead's, so ten salons in a row do
      not all get the same graphic. Unticking here narrows every industry at
      once &mdash; the one place to trim the series down to the looks that
      work. Tick none and all of them stay in play.</p>
    <div class="sub" style="margin-bottom:4px"><b>Style</b></div>
    <div id="rot-styles" class="row" style="gap:12px;flex-wrap:wrap"></div>
    <div class="sub" style="margin:10px 0 4px"><b>Layout</b></div>
    <div id="rot-layouts" class="row" style="gap:12px;flex-wrap:wrap"></div>
    <div class="row" style="margin-top:10px">
      <button onclick="saveRotation()">Save rotation</button>
      <span class="hint" id="rot-note"></span>
    </div>
  </details>
  <div id="visual"></div>

  <h2 id="send-step"><span class="stepno">4</span> Send
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

// A panel that cannot reach the server must say so. Returning quietly
// leaves it on "Loading..." for ever, which reads as the whole dashboard
// being broken with nothing at all to go on -- and the server is usually
// fine, having simply been stopped or not started.
function panelDead(sel, what) {
  const el = $(sel);
  if (el) {
    el.innerHTML = '<div class="card"><span class="err">Cannot reach the '
      + 'dashboard server for ' + esc(what) + '.</span>'
      + '<p class="hint">The page is open but nothing is answering it. '
      + 'Check the Dashboard.bat window is still running, then reload. '
      + 'If it closed, the error will be in that window.</p></div>';
  }
}

async function createSheet() {
  const btn = $('#mk-sheet');
  const note = $('#mk-sheet-note');
  btn.disabled = true;
  note.textContent = 'Opening Google to sign in\u2026 finish in the browser window.';
  try {
    const r = await fetch('/api/create-sheet', {method: 'POST'});
    const d = await r.json();
    if (d.error) { note.innerHTML = '<span class="err">' + esc(d.error) + '</span>'; return; }
    note.innerHTML = 'Made it \u2014 <a href="' + esc(d.url) +
      '" target="_blank" rel="noopener">open your Sheet</a>';
    refresh();
  } catch (e) {
    note.innerHTML = '<span class="err">Could not reach the dashboard.</span>';
  } finally {
    btn.disabled = false;
  }
}

async function refresh() {
  let d;
  try { d = await (await fetch('/api/summary')).json(); } catch { panelDead('#totals', 'the totals'); return; }

  if (d.error) {
    $('#totals').innerHTML = `<div class="long-err">${esc(d.error)}</div>`;
    return;
  }

  if (d.setup_needed) {
    setupReady = false;
    $('#totals').innerHTML = '<span class="err">Not set up yet</span>';

    // The Sheet is the one missing piece we can just make for them. The
    // keys have to be pasted, but nobody should have to create a
    // spreadsheet by hand and copy an id out of a URL.
    const needsSheet = d.setup_needed.includes('SHEET_ID');
    const keys = d.setup_needed.filter(k => k !== 'SHEET_ID');

    $('#todo').innerHTML = `<div class="card" style="grid-column:1/-1">
      <div class="what"><b>First run &mdash; three things to set up.</b></div>

      <div class="sub" style="margin:10px 0 4px"><b>1. Your own Google Sheet</b></div>
      ${needsSheet
        ? `<div class="sub">This is where your leads live. Press the button and
             sign in with your own Google account &mdash; the Sheet is made in
             your account, with the right tabs, and remembered for you.</div>
           <div class="row" style="margin:8px 0 14px">
             <button class="primary" onclick="createSheet()" id="mk-sheet">Create my Google Sheet</button>
             <span class="hint" id="mk-sheet-note"></span>
           </div>`
        : '<div class="sub" style="margin-bottom:14px">Done.</div>'}

      <div class="sub" style="margin:0 0 4px"><b>2. Your keys</b></div>
      ${keys.length
        ? `<div class="sub">Still missing: <b>${keys.join('</b>, <b>')}</b>.
             Put them in the <b>Model</b> and settings panels on this page &mdash;
             they are written for you, no file to edit. The
             <a href="/guide#getting-it-onto-your-computer" target="_blank"
                rel="noopener">handbook</a> says where each one comes from.</div>`
        : '<div class="sub">Done.</div>'}

      <div class="sub" style="margin:14px 0 4px"><b>3. Your WhatsApp</b></div>
      <div class="sub">Once the two above are done, section 4 shows a QR code.
        Scan it from WhatsApp &rarr; Linked devices. It links your own phone,
        and only needs doing once.</div>
      </div>`;
    $('#funnel').innerHTML = '';
    $('#send').innerHTML = '<div class="sub">Nothing yet.</div>';
    return;
  }

  // Setup is complete: the next poll tick re-arms the stage buttons it
  // disabled when the page was still missing its keys.
  setupReady = true;
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

// Per-lead interface state, keyed by Lead ID: whether a card is waiting for
// its confirm, mid-send, sent, failed, and any edit made to its text.
//
// It is keyed by lead rather than kept on the queue objects because
// loadWhatsAppInline replaces the whole array with fresh objects from the
// server -- so a flag set on a queue object was thrown away by the next
// refresh, which runs on a 30-second timer. That is why pressing Send twice
// sometimes did nothing at all: the first press armed the confirmation, a
// refresh landed in between and discarded it, and the second press only
// armed it again. The same discard is why the Sending and Sent states
// never appeared.
const waUi = {};

function uiFor(leadId) {
  if (!waUi[leadId]) waUi[leadId] = {};
  return waUi[leadId];
}

// A DOM-safe id per lead. Lead IDs are Google place_ids, but they are not
// ours to assume anything about, so anything outside the safe set is
// replaced rather than trusted in an id or a selector.
function waKey(leadId) {
  return String(leadId || '').replace(/[^A-Za-z0-9_-]/g, '_');
}

// The click-to-chat link for one card, carrying whatever is in the box.
//
// The number comes from the server and nothing here touches it: the base of
// wa_link is already `https://wa.me/<digits>`, worked out where the lead's
// own country was known. Only the text is replaced, because the textarea is
// editable and a link built once server-side would quietly send the draft
// as it was before the edit. Everything after `?text=` is percent-encoded,
// so the finished URL cannot carry a quote into the href it lands in.
function waHandLink(m, text) {
  if (!m.wa_link) return '';
  return m.wa_link.split('?')[0] + '?text=' + encodeURIComponent(text || '');
}

// True while any card is mid-interaction. The periodic refresh stands aside
// for it: redrawing then would discard a pending confirmation and any edit
// made to the message text.
function waBusy() {
  return Object.values(waUi).some(
    u => u.confirming || u.sending || u.handed || u.draft !== undefined
  );
}

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
  const dot = waState.connected ? 'on'
    : (waState.reconnecting || waState.resetting || waState.connecting) ? 'wait'
    : waState.paired === false ? 'wait' : 'off';
  // last_error is the relay's own account of what went wrong, and it beats
  // "Relay not running" -- which describes the symptom of a crash, a wrong
  // port and a relay never started all in the same four words.
  // A relay working its way back is not one that has given up, and saying
  // "not connected" for both is what made a passing network blip look like
  // something broken that needed a restart.
  const label = waState.connected
    ? `Connected \\u2014 ${waState.daily_sent || 0} / ${waState.daily_cap || 30} sent today`
    : waState.resetting ? 'Resetting\\u2026'
    : waState.connecting ? 'Connecting to WhatsApp\\u2026'
    : waState.reconnecting
      ? `Reconnecting\\u2026 next try in ${waState.retry_in_seconds || 0}s`
        + (waState.last_error ? ' \\u2014 ' + waState.last_error : '')
    : waState.last_error || waState.error || 'Relay not running';
  // Offered whenever the relay itself answered -- connected or not. An
  // unpaired relay showing a QR that has gone stale is exactly when someone
  // needs this, so gating it on "connected" would hide it in the one state
  // it is most wanted.
  const canReset = waState.enabled !== false && !waState.error;
  const resetBtn = canReset
    ? `<button class="wa-reset" id="wa-reset-btn" onclick="resetWhatsApp()"
         title="Unlink this WhatsApp account and show a new QR code, so you can sign in again or link a different phone.">Log out / switch account</button>`
    : '';
  html += `<div class="wa-status"><span class="wa-dot ${dot}"></span>${esc(label)}${resetBtn}</div>`;
  html += `<div class="wa-reset-note" id="wa-reset-note"></div>`;

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
    html += waQueue.map((m) => {
      const k = waKey(m.lead_id);
      const u = uiFor(m.lead_id);
      // The edit in progress wins over the server's copy, so a refresh
      // landing mid-typing does not wipe what has been written.
      const msgText = esc(u.draft !== undefined ? u.draft : (m.message || ''));
      let actions = '';
      if (u.confirming) {
        actions = `<span class="wa-state">Send to ${esc(m.phone)}?</span>
          <button class="wa-send confirm" onclick="sendWhatsApp('${esc(m.lead_id)}')">Yes, send</button>
          <button onclick="cancelSend('${esc(m.lead_id)}')">Cancel</button>`;
      } else if (u.sending) {
        actions = `<span class="wa-state"><span class="wa-spin"></span>Sending\\u2026</span>`;
      } else if (u.sent) {
        actions = u.note
          ? `<span class="wa-state ok" title="${esc(u.note)}">Sent \\u2714 (not yet confirmed)</span>`
          : `<span class="wa-state ok">Sent \\u2714</span>`;
      } else if (u.failed) {
        actions = `<span class="wa-state err">${esc(u.error || 'Failed')}</span>
          <button class="wa-send" onclick="retryWhatsApp('${esc(m.lead_id)}')">Retry</button>
          <button class="wa-skip" onclick="skipWhatsApp('${esc(m.lead_id)}')"
            title="Take this one off the list without sending. It keeps its message and its site, and Send again below puts it back.">Not this one</button>`;
      } else if (u.handed) {
        // WhatsApp opened in another app or another tab and told this page
        // nothing at all, so the only thing that knows whether it went is
        // the person who pressed it. Asking is not a formality: leaving the
        // card alone would keep offering the same business tomorrow.
        actions = `<div class="wa-actions">
          <span class="wa-state">Did it send?</span>
          <button class="wa-send" onclick="markSentWhatsApp('${esc(m.lead_id)}')">Yes, it sent</button>
          <button onclick="cancelHandOff('${esc(m.lead_id)}')">Not yet</button>
        </div>`;
      } else {
        // Two ways to send, and which one leads depends on whether the
        // relay is there. On a hosted deployment it never is -- a linked
        // phone needs a machine that stays awake, and putting that in a
        // data centre risks the number rather than protecting it -- so the
        // relay button is not merely unhelpful there, it is a button that
        // cannot work. It is left out entirely in that case rather than
        // offered and failed.
        const handLink = waHandLink(m, u.draft !== undefined ? u.draft : (m.message || ''));
        const hand = handLink
          ? `<a class="wa-hand" href="${handLink}" target="_blank" rel="noopener"
               onclick="handOffWhatsApp('${esc(m.lead_id)}')"
               title="Opens WhatsApp with this message already written. You press send there.">Send on my phone</a>`
          : '';
        const relay = waState && waState.connected
          ? `<button class="wa-send" onclick="sendWhatsApp('${esc(m.lead_id)}')">Send from here</button>`
          : '';
        actions = `<div class="wa-actions">
          <button class="wa-rewrite" onclick="rewriteWhatsApp('${esc(m.lead_id)}')">Rewrite</button>
          <button class="wa-skip" onclick="skipWhatsApp('${esc(m.lead_id)}')"
            title="Take this one off the list without sending. It keeps its message and its site, and Send again below puts it back.">Not this one</button>
          ${hand}${relay}
        </div>`;
      }
      // The picture that will ride with the message. Shown small, and
      // clickable, because the only way to check it before was to send it
      // and look at your phone.
      // Named, not assumed. The queue has always said which picture it is
      // carrying; the page said "site screenshot" whatever arrived, so an
      // infographic -- which IS the pitch on that track -- was described as
      // something else entirely.
      const shotWord = m.shot_kind === 'visual'
        ? 'Infographic attached' : 'Site screenshot attached';
      const shot = m.has_shot
        ? `<a href="/api/screenshot/${encodeURIComponent(m.lead_id)}" target="_blank"
              rel="noopener" class="wa-shot" title="Open the picture full size">
             <img src="/api/screenshot/${encodeURIComponent(m.lead_id)}" alt="">
             <span>${shotWord}</span></a>`
        : '<div class="sub" style="margin:6px 0">No picture &mdash; the text '
          + 'will send on its own.</div>';

      // Written for the page pitch and never rewritten. The campaign now
      // sends a picture and nothing else, so the link in this text offers
      // something this message is not about -- and to a business that has
      // very likely already read it once.
      const stale = m.stale_pitch
        ? `<div class="long-err" style="margin:6px 0">This message was written
             for the website pitch and still links to the demo site. The
             campaign sends the picture on its own now. Press
             <b>Rewrite</b> before sending it.</div>`
        : '';
      return `<div class="wa-msg-card" id="wa-msg-${k}">
        <div class="wa-name">${esc(m.name)}</div>
        <div class="wa-phone">${esc(m.phone)}</div>
        ${shot}
        ${stale}
        <textarea class="wa-edit" id="wa-text-${k}" rows="4"
          oninput="noteEdit('${esc(m.lead_id)}', this.value)">${msgText}</textarea>
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
        (x.status || '').toLowerCase() === 'skipped'
          ? 'Skipped' : `Status: ${x.status || 'blank'}`,
        (x.status || '').toLowerCase() === 'skipped'
          ? 'You took this one off the send list. Nothing was sent, and it still has its message and its site \\u2014 press Send again to put it back.'
          : `This lead has a drafted message, but its Status cell says \\u2018${x.status || ''}\\u2019 rather than \\u2018Drafted\\u2019. Press Send again to put it back in the list.`],
    };
    // Only these two are undoable by clearing a cell. The rest -- no phone,
    // no Leads row, drafted as email -- need a different fix each, and a
    // button promising to undo them would simply not work.
    const UNDOABLE = {already_sent: 1, status_not_drafted: 1};
    const items = waExcluded.map((x, i) => {
      const pair = (reasons[x.reason] || (() => ['Hidden', 'Hidden from the send list.']))(x);
      const undo = UNDOABLE[x.reason]
        ? `<button class="wa-undo" id="wa-undo-${i}"
             onclick="undoSend(${i}, '${esc(x.lead_id)}')"
             title="Put this back in the send list so you can send it again. The message and its screenshot are kept.">Send again</button>`
        : '';
      return `<div class="wa-excl" title="${esc(x.name || x.lead_id)} \\u2014 ${esc(pair[1])}">
        <div class="wa-name">${esc(x.name || x.lead_id)}</div>
        <div class="wa-why">${esc(pair[0])}</div>
        ${undo}
      </div>`;
    }).join('');
    html += `<div class="wa-excl-wrap">
      <div class="wa-excl-head">${waExcluded.length} drafted message${waExcluded.length > 1 ? 's' : ''} you can\\u2019t send from here \\u2014 hover any for the reason</div>
      <div class="wa-excl-grid">${items}</div>
    </div>`;
  }

  el.innerHTML = html;
}

// Put an already-sent lead back in the send list.
//
// A message can be acknowledged by WhatsApp and still never arrive -- that
// is what the "Waiting for this message" ones were. The Sheet then says sent
// when it is not, and correcting it used to mean opening the Sheet and
// clearing a cell by hand.
async function undoSend(idx, leadId) {
  const btn = $(`#wa-undo-${idx}`);
  if (btn) { btn.disabled = true; btn.textContent = 'Working\\u2026'; }

  let d;
  try {
    d = await (await fetch('/api/whatsapp/undo-send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: leadId}),
    })).json();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Send again'; }
    return;
  }

  if (d.error) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Send again';
      btn.title = d.error;
      btn.classList.add('err');
    }
    return;
  }

  // Forget how the previous send ended. The card is about to reappear in the
  // queue, and a leftover "Sent" from the attempt being undone would render
  // it as already done.
  delete waUi[leadId];

  // Redraw the whole panel: the lead has moved out of the excluded grid and
  // into the queue above, which is the confirmation that it worked.
  await loadWhatsAppInline();
  refresh();
}

// Unlink the paired account and wait for the new QR.
//
// Two presses on purpose. This is not destructive to any data, but it does
// end a link that takes a phone in hand to restore -- and it sits a few
// pixels from the Send buttons.
let waResetting = false;

async function resetWhatsApp() {
  const btn = $('#wa-reset-btn');
  const note = $('#wa-reset-note');
  if (!note) return;

  if (!waResetting) {
    waResetting = true;
    note.innerHTML = `<b>Log out of WhatsApp on this computer?</b>
      Messages already sent stay in your phone. You will scan a new QR code to
      link again \u2014 this phone, or a different one.
      <button class="wa-send confirm" onclick="resetWhatsApp()">Yes, log out</button>
      <button onclick="cancelWhatsAppReset()">Cancel</button>`;
    return;
  }

  waResetting = false;
  if (btn) btn.disabled = true;
  note.textContent = 'Logging out\u2026';

  let d;
  try {
    d = await (await fetch('/api/whatsapp/reset', {method: 'POST'})).json();
  } catch (e) {
    note.innerHTML = '<span class="err">Could not reach the dashboard.</span>';
    if (btn) btn.disabled = false;
    return;
  }

  if (d.error) {
    note.innerHTML = '<span class="err">' + esc(d.error) + '</span>';
    if (btn) btn.disabled = false;
    return;
  }

  // The relay reconnects in the background, so the QR is not there the
  // instant this returns. Poll rather than redraw once onto an empty box
  // and leave the user thinking it failed.
  note.textContent = 'Logged out. Waiting for the new QR code\u2026';
  for (let i = 0; i < 12; i++) {
    await new Promise(r => setTimeout(r, 1500));
    let qr = null;
    try { qr = (await (await fetch('/api/whatsapp/qr')).json()).qr; } catch {}
    if (qr) {
      await loadWhatsAppInline();
      const n = $('#wa-reset-note');
      if (n) n.textContent = 'Scan the code below with the phone you want to use.';
      return;
    }
  }

  await loadWhatsAppInline();
  const n = $('#wa-reset-note');
  if (n) {
    n.innerHTML = '<span class="err">Logged out, but no new QR code appeared. '
      + 'Restart the relay window and reload this page.</span>';
  }
}

function cancelWhatsAppReset() {
  waResetting = false;
  const note = $('#wa-reset-note');
  if (note) note.textContent = '';
}

function noteEdit(leadId, value) {
  uiFor(leadId).draft = value;
}

// Look the message up by Lead ID, never by position. The queue is rebuilt
// from the server on a timer, and a card's index moves the moment anything
// enters or leaves the list -- so an index captured when the button was
// drawn could address a different business by the time it was pressed.
function waMessage(leadId) {
  return waQueue.find(m => m.lead_id === leadId);
}

function sendWhatsApp(leadId) {
  const msg = waMessage(leadId);
  if (!msg) return;
  const u = uiFor(leadId);

  // First press: arm the confirmation. It lives in waUi, so a refresh
  // landing before the second press cannot quietly discard it.
  if (!u.confirming) {
    u.confirming = true;
    loadWhatsAppInline();
    return;
  }

  const message = u.draft !== undefined ? u.draft : msg.message;
  u.confirming = false;
  u.sending = true;
  loadWhatsAppInline();

  fetch('/api/whatsapp/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    // phone is the display form; phone_e164 is what actually gets dialled.
    body: JSON.stringify({lead_id: leadId, phone: msg.phone,
                          phone_e164: msg.phone_e164 || '', message: message}),
  }).then(r => r.json()).then(d => {
    u.sending = false;
    if (d.error) { u.failed = true; u.error = d.error; }
    else {
      u.sent = true;
      u.note = d.note || '';
      // The edit has been sent, so it is no longer an unsaved change and
      // must not hold off the refresh that moves this card to the sent list.
      delete u.draft;
    }
    loadWhatsAppInline();
    refresh();  // update counts
  }).catch(e => {
    u.sending = false;
    u.failed = true;
    u.error = e.message || 'Network error';
    loadWhatsAppInline();
  });
}

// Take one message off the send list without sending it.
//
// Not a disqualification: the lead keeps everything, the row moves down to
// the excluded list, and Send again there puts it straight back. That undo
// is why this does not stop to confirm.
function skipWhatsApp(leadId) {
  const card = $(`#wa-msg-${waKey(leadId)}`);
  const btn = card && card.querySelector('.wa-skip');
  if (btn) { btn.disabled = true; btn.textContent = 'Skipping\\u2026'; }

  fetch('/api/whatsapp/skip', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({lead_id: leadId}),
  }).then(r => r.json()).then(d => {
    if (d.error) {
      const row = card && card.querySelector('.wa-row');
      if (row) row.innerHTML = `<span class="wa-state err">${esc(d.error)}</span>`;
      return;
    }
    // Nothing of this card's state should follow it into the excluded list.
    delete waUi[leadId];
    loadWhatsAppInline();
    refresh();
  }).catch(e => {
    if (btn) { btn.disabled = false; btn.textContent = 'Not this one'; }
  });
}

function cancelSend(leadId) {
  uiFor(leadId).confirming = false;
  loadWhatsAppInline();
}

// --- sending it yourself, from the phone this page is open on

function handOffWhatsApp(leadId) {
  // The link's own navigation is left alone -- this only remembers that it
  // was followed, so the card can ask afterwards. Returning false here, or
  // calling preventDefault, would stop WhatsApp opening at all.
  uiFor(leadId).handed = true;
  // Redrawing immediately would replace the anchor mid-click on some
  // browsers and lose the navigation with it.
  setTimeout(() => loadWhatsAppInline(), 800);
}

function cancelHandOff(leadId) {
  uiFor(leadId).handed = false;
  loadWhatsAppInline();
}

async function markSentWhatsApp(leadId) {
  const u = uiFor(leadId);
  u.handed = false;
  u.sending = true;
  loadWhatsAppInline();
  let d;
  try {
    d = await (await fetch('/api/whatsapp/mark-sent', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: leadId}),
    })).json();
  } catch (e) { d = {error: 'Could not reach the dashboard'}; }
  u.sending = false;
  if (d.error) {
    u.failed = true;
    u.error = d.error;
  } else {
    u.sent = true;
    // Said out loud, because it is a claim rather than something observed:
    // this page never saw the message leave.
    u.note = 'Recorded from your phone. Send again puts it back if it did not arrive.';
  }
  loadWhatsAppInline();
}

function retryWhatsApp(leadId) {
  const u = uiFor(leadId);
  u.failed = false;
  u.error = null;
  loadWhatsAppInline();
}

function rewriteWhatsApp(leadId) {
  const card = $(`#wa-msg-${waKey(leadId)}`);
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
    // The server has written a new message, so any local edit is stale.
    delete uiFor(leadId).draft;
    loadWhatsAppInline();
  }).catch(e => {
    const row = card.querySelector('.wa-row');
    if (row) row.innerHTML = `<span class="wa-state err">${esc(e.message || 'Network error')}</span>`;
  });
}

// Poll WhatsApp state every 30 seconds -- but never over the top of someone
// mid-decision. A redraw while a confirmation is armed or a message is being
// edited throws that away, and the press that follows lands on a card that
// has quietly reset itself.
if (WA_ENABLED) {
  loadWhatsAppInline();
  setInterval(() => { if (!waBusy()) loadWhatsAppInline(); }, 30000);
}

// First-run setup state. While keys or the Sheet are missing, the stage
// buttons stay dead -- pressing one only fails at the stage with an error
// naming a key, which reads like the app is broken. The setup card above
// is the way out. Checking setup and testing the model are exempt: they
// are part of getting set up.
const SETUP_LOCKED = new Set(['full', 'discovery', 'discovery-refresh', 'research',
  'reachability', 'reachability-recheck', 'site-brief', 'site-build',
  'publish', 'outreach-draft', 'site-brief-rewrite', 'site-generate']);
let setupReady = true;

async function poll() {
  let d;
  try { d = await (await fetch('/api/log')).json(); } catch { return; }
  if (d.lines.length) $('#log').textContent = d.lines.join('\\n');
  $('#log').scrollTop = $('#log').scrollHeight;
  const busy = !!d.running;
  $('#running').textContent = busy ? `Running ${d.running}\\u2026` : '';
  document.querySelectorAll('button[data-run]').forEach(b => b.disabled = busy);
  document.querySelectorAll('button[data-run]').forEach(b => {
    if (!SETUP_LOCKED.has(b.dataset.run)) return;
    b.disabled = busy || !setupReady;
  });
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
  try { modelInfo = await (await fetch('/api/model')).json(); } catch { panelDead('#model', 'the model settings'); return; }
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
    <input id="m-model" style="width:100%;margin:5px 0 4px" value="${esc(m.model)}">
    <p class="hint" style="margin:0 0 8px">Used for research, the site brief and
      the infographic prompt. No example is printed here on purpose: a model
      name that is right today stops working without warning, so the only
      honest list is the one your own key answers with.</p>
    <div class="row" style="margin:0 0 6px">
      <button type="button" onclick="loadCatalogue()">Show the models my key has</button>
      <span class="sub" id="m-cat-msg"></span>
    </div>
    <div id="m-catalogue" style="margin:0 0 14px"></div>
    <label class="sub" for="m-omodel">Message model <span class="sub">(optional)</span></label>
    <input id="m-omodel" style="width:100%;margin:5px 0 4px" value="${esc(m.outreach_model || '')}"
           placeholder="blank = use the model above">
    <p class="hint" style="margin:0 0 14px">A smaller model that only writes the
      email + WhatsApp text &mdash; <b>every</b> message, the website ones and
      the infographic ones alike. There is no separate model for the picture
      messages. Blank means it uses the model above. Set this to cut the token
      cost of drafting.</p>
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

async function loadCatalogue() {
  const box = $('#m-catalogue'), note = $('#m-cat-msg');
  if (!box) return;
  note.textContent = 'Asking your provider\u2026';
  box.innerHTML = '';
  let d;
  try {
    d = await (await fetch('/api/model/catalogue')).json();
  } catch { d = {error: 'Could not reach the dashboard'}; }
  if (d.error) {
    note.textContent = '';
    box.innerHTML = `<div class="long-err">${esc(d.error)}</div>`;
    return;
  }
  const names = d.models || [];
  if (!names.length) {
    note.textContent = '';
    box.innerHTML = '<div class="sub">Your key sees no models at all. '
      + 'That usually means the key belongs to a different provider.</div>';
    return;
  }
  note.textContent = names.length + ' from ' + d.provider;
  // Clickable, because the whole point is that nobody should be retyping a
  // name they read off a screen -- that is how a typo becomes a 404 that
  // looks like a retired model.
  box.innerHTML = '<div style="max-height:220px;overflow:auto;border:1px solid var(--line);'
    + 'border-radius:8px;padding:6px">'
    + names.map(n => `<div class="row" style="justify-content:space-between;gap:8px;padding:2px 0">
         <code style="overflow-wrap:anywhere">${esc(n)}</code>
         <span class="row" style="gap:6px">
           <button type="button" onclick="pickModel('m-model', '${esc(n)}')">Use</button>
           <button type="button" onclick="pickModel('m-omodel', '${esc(n)}')">For messages</button>
         </span></div>`).join('')
    + '</div>';
}

function pickModel(field, name) {
  const el = $('#' + field);
  if (el) { el.value = name; el.focus(); }
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
  try { d = await (await fetch('/api/next-up' + q)).json(); } catch { panelDead('#nextup', 'what needs you today'); return; }
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
  await loadNextUp();
  await refresh();
}

// -------------------------------------------------------- what you're hunting

let target = null;

async function loadTarget() {
  try { target = await (await fetch('/api/campaign')).json(); } catch { panelDead('#target', 'the campaign'); return; }
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
  try { d = await (await fetch('/api/reachability')).json(); }
  catch { panelDead('#reach', 'the WhatsApp check'); return; }
  if (d.error) { el.innerHTML = `<span class="err">${esc(d.error)}</span>`; return; }

  const c = d.counts || {};
  if (!c.verified && !c.rejected && !c.waiting) {
    el.innerHTML = '<div class="what">Nothing researched yet.</div>' +
      '<p class="hint">Run <b>Research</b> and the WhatsApp check runs with it.</p>';
    return;
  }

  // `drop` adds a per-row Remove button. Only the "not on WhatsApp" list
  // gets one: those are the answered dead ends, and removing a lead that is
  // merely unchecked would throw away one the relay has not answered for yet.
  const rows = (list, drop) => list.map((e, i) =>
    `<tr id="reach-row-${drop ? 'x' : 'v'}-${i}">` +
    `<td style="padding:2px 10px 2px 0">${esc(e.name)}</td>` +
    `<td style="white-space:nowrap;padding:2px 0">${esc(e.dialled || e.phone)}</td>` +
    `<td style="padding:2px 0 2px 10px">${e.email ? 'has email' : ''}</td>` +
    (drop
      ? `<td style="padding:2px 0 2px 10px;text-align:right">` +
        `<button class="reach-drop" id="reach-drop-${i}"
           onclick="dropUnreachable(${i}, '${esc(e.lead_id)}', '${esc(e.name)}', ${e.email ? 1 : 0})"
           title="Disqualify this lead so it is never researched or built.">Remove</button></td>`
      : '') +
    `</tr>`
  ).join('');

  // Only the ones needing a decision are shown outright. The verified and
  // rejected lists are the long ones and are answered questions, so they
  // fold away -- the counts are what Lyon actually reads each morning.
  const fold = (label, list, drop) => list.length
    ? `<details style="margin-top:8px"><summary class="sub">${label} (${list.length})</summary>` +
      (drop
        ? '<p class="hint" style="margin:6px 0 0">These have no WhatsApp account. ' +
          '<b>Remove</b> disqualifies one for good, so it is never researched or ' +
          'built. A lead marked <b>has email</b> can still be reached that way &mdash; ' +
          'removing it gives that up too.</p>'
        : '') +
      `<table style="width:100%;border-collapse:collapse;margin-top:6px">${rows(list, drop)}</table></details>`
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
    fold('Not on WhatsApp', d.rejected || [], true);
}

// Disqualify one lead from the "not on WhatsApp" list.
//
// Two presses. It is terminal -- Disqualified is a stage nothing moves a
// lead out of -- and the rows sit close together in a narrow table.
let reachDropping = null;

async function dropUnreachable(idx, leadId, name, hasEmail) {
  const btn = $(`#reach-drop-${idx}`);
  const row = $(`#reach-row-x-${idx}`);
  if (!btn || !row) return;

  if (reachDropping !== idx) {
    reachDropping = idx;
    btn.textContent = hasEmail ? 'Remove anyway?' : 'Sure?';
    btn.title = hasEmail
      ? `${name} has an email address and could still be contacted that way.`
      : `Disqualify ${name} for good.`;
    btn.classList.add('confirm');
    return;
  }

  reachDropping = null;
  btn.disabled = true;
  btn.textContent = 'Removing\\u2026';

  let d;
  try {
    d = await (await fetch('/api/drop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: leadId}),
    })).json();
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Remove';
    return;
  }

  if (d.error) {
    btn.disabled = false;
    btn.textContent = 'Remove';
    btn.title = d.error;
    btn.classList.add('err');
    return;
  }

  // Redrawn rather than hidden: the counts at the top of the panel move too,
  // and "buildable" is the number that decides spend.
  await loadReach();
  refresh();
}

// How much art direction the site prompt carries. A campaign setting rather
// than a per-lead one -- every business gets the same craft rules.
async function loadDesignLevel() {
  const box = $('#design-showcase');
  if (!box) return;
  try {
    const d = await (await fetch('/api/design-level')).json();
    box.checked = d.level === 'showcase';
  } catch { /* leave it alone rather than showing a wrong state */ }
}

async function setDesignLevel(on) {
  const box = $('#design-showcase');
  const note = $('#design-note');
  const wanted = on ? 'showcase' : 'standard';
  if (box) box.disabled = true;
  if (note) note.textContent = 'Saving\\u2026';

  let d;
  try {
    d = await (await fetch('/api/design-level', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({level: wanted}),
    })).json();
  } catch (e) {
    if (note) note.innerHTML = '<span class="err">Could not reach the dashboard.</span>';
    if (box) { box.disabled = false; box.checked = !on; }
    return;
  }

  if (box) box.disabled = false;
  if (d.error) {
    if (note) note.innerHTML = '<span class="err">' + esc(d.error) + '</span>';
    // Put the box back where it was: the setting did not change.
    if (box) box.checked = !on;
    return;
  }
  if (note) {
    note.textContent = on
      ? 'On \\u2014 briefs written from now on carry it.'
      : 'Off \\u2014 back to the shorter prompt.';
  }
}

// The services listed at the end of every message. A campaign constant --
// every lead gets the same list -- but the wording is the part worth tuning
// after reading a few replies, so it is editable here rather than in YAML.
async function loadServices() {
  const intro = $('#svc-intro');
  const list = $('#svc-list');
  if (!intro || !list) return;
  try {
    const d = await (await fetch('/api/services')).json();
    intro.value = d.intro || '';
    list.value = (d.services || []).join('\\n');
  } catch { /* leave the boxes alone rather than showing a wrong state */ }
}

async function saveServices() {
  const note = $('#svc-note');
  const intro = $('#svc-intro').value;
  const services = $('#svc-list').value.split('\\n');
  if (note) note.textContent = 'Saving\\u2026';

  let d;
  try {
    d = await (await fetch('/api/services', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({intro: intro, services: services}),
    })).json();
  } catch (e) {
    if (note) note.innerHTML = '<span class="err">Could not reach the dashboard.</span>';
    return;
  }

  if (d.error) {
    if (note) note.innerHTML = '<span class="err">' + esc(d.error) + '</span>';
    return;
  }
  if (note) {
    note.textContent = d.services.length
      ? `Saved \\u2014 ${d.services.length} service(s) on every message from now on.`
      : 'Saved \\u2014 no services listed, messages end as before.';
  }
  loadServices();
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
      <h3>${esc(q.name)}</h3>
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

// ---------------------------------------------------------- infographic
// The picture is made outside this tool, so this panel is the hand-off in
// both directions: the prompt out, the image back. Four states, four
// different buttons -- waiting for a picture, waiting for someone to read
// the words on it, ready, and retired by hand.
let vizQueue = [];
// Transient state lives here, keyed by Lead ID, never on the queue objects
// -- a 30-second refresh replaces those, which is how a first press used to
// arm a confirmation that the next tick silently threw away.
const vizUi = {};

function vizFor(leadId) {
  if (!vizUi[leadId]) vizUi[leadId] = {};
  return vizUi[leadId];
}

function vizBusy() {
  // The timer stands aside while a card is mid-decision, so a link being
  // typed or a confirmation being read is not wiped by a background load.
  return Object.values(vizUi).some(u => u.busy || u.armed || u.confirming);
}

// Which states the list is showing. Sixty cards is not a list anybody reads,
// and three of the four states need nothing from him -- so the default is
// the one that does.
let vizFilter = 'needs_image';
let vizData = null;

function vizSetFilter(name) {
  vizFilter = name;
  if (vizData) vizDraw(vizData);
}

function vizToggle(leadId) {
  const u = vizFor(leadId);
  u.open = !u.open;
  if (vizData) vizDraw(vizData);
}

async function loadVisual() {
  let d;
  try { d = await (await fetch('/api/visual/queue')).json(); } catch { return; }
  const el = document.querySelector('#visual');
  const step = document.querySelector('#visual-step');
  const controls = document.querySelector('#visual-controls');
  if (!el) return;
  if (d.catalogue) drawRotation(d.catalogue);
  const rot = document.querySelector('#rotation-settings');
  if (rot) rot.style.display = d.enabled ? '' : 'none';
  const pick = document.querySelector('#visual-pick');
  if (pick) pick.style.display = d.enabled ? '' : 'none';
  if (!d.enabled) {
    // Hidden rather than shown empty. A campaign pitching a page has no use
    // for this step, and a panel that can never do anything reads as broken.
    if (step) step.style.display = 'none';
    if (controls) controls.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  if (step) step.style.display = '';
  if (controls) controls.style.display = '';
  if (d.error) {
    // .long-err, not a plain span: this message is several lines and HTML
    // collapses every newline inside one, which is how a three-step cure
    // once reached the page as a single run-on paragraph.
    el.innerHTML = `<div class="card">
      <div class="long-err">${esc(d.error)}</div>
      ${d.missing_tab ? repairTabsHtml() : ''}</div>`;
    return;
  }
  vizData = d;
  vizQueue = d.queue || [];
  loadVisualCandidates();
  vizDraw(d);
}

function vizDraw(d) {
  const el = document.querySelector('#visual');
  if (!el) return;
  const c = d.counts || {};
  const note = document.querySelector('#visual-note');
  if (note) {
    note.textContent = (d.queue || []).length
      ? `${c.needs_image || 0} need a picture, ${c.needs_check || 0} need checking, ${c.ready || 0} ready`
      : '';
  }
  if (!(d.queue || []).length) {
    el.innerHTML = '<div class="card"><div class="what">No image prompts yet.</div>' +
      '<div class="sub">Pick a group above and press Write image prompts. It uses ' +
      'the research you already have, so it costs one small model call per lead.</div></div>';
    return;
  }
  // Removed is last and never part of All: "remove" has to mean gone from
  // the list, or the button has not done what it says.
  // Sent is its own list and, like Removed, is not part of All: a graphic
  // that has reached a business is finished work, and leaving it under
  // Ready made that list grow by one every time a message went out.
  const tabs = [
    ['needs_image', 'Need a picture', c.needs_image || 0],
    ['needs_check', 'Need checking', c.needs_check || 0],
    ['ready', 'Ready to send', c.ready || 0],
    ['all', 'All', (c.needs_image || 0) + (c.needs_check || 0) + (c.ready || 0)],
    ['sent', 'Sent', c.sent || 0],
    ['skipped', 'Removed', c.skipped || 0],
  ];
  const bar = tabs.map(t => `<button onclick="vizSetFilter('${t[0]}')"
      class="${vizFilter === t[0] ? '' : 'ghost'}"
      ${t[2] ? '' : 'disabled'}>${t[1]} (${t[2]})</button>`).join(' ');

  const shown = vizQueue.filter(m =>
    vizFilter === 'all'
      ? (m.state !== 'skipped' && m.state !== 'sent')
      : m.state === vizFilter);

  el.innerHTML = `<div class="row" style="margin-bottom:10px;flex-wrap:wrap">${bar}</div>` +
    (shown.length
      ? shown.map(m => vizCard(m, d)).join('')
      : `<div class="card"><div class="sub">Nothing in this list.</div></div>`);
}

function vizCard(m, d) {
  const u = vizFor(m.lead_id);
  const id = encodeURIComponent(m.lead_id);
  const open = !!u.open;
  const states = {
    needs_image: 'needs a picture',
    needs_check: 'needs checking',
    ready: 'ready to send',
    skipped: 'removed',
  };
  const badge = m.specific
    ? '<span class="sub">named</span>'
    : '<span class="sub viz-generic">generic</span>';
  // Collapsed by default. The header carries everything needed to decide
  // whether to open one -- who it is, what it is waiting for, and whether it
  // claims anything about them.
  const head = `<div class="viz-head" onclick="vizToggle('${id}')"
       title="${open ? 'Hide' : 'Show'} this one">
      <span class="viz-caret">${open ? '&#9662;' : '&#9656;'}</span>
      <b>${esc(m.name)}</b>
      <span class="sub">${esc(states[m.state] || m.state)}</span>
      ${badge}
    </div>`;
  if (!open) return `<div class="card q viz-row">${head}</div>`;
  return `<div class="card q viz-row">${head}${vizBody(m, d, id, u)}</div>`;
}

function vizBody(m, d, id, u) {
  const tool = d.tool_name || 'the image tool';
  const openBtn = d.tool_url
    ? `<a href="${esc(d.tool_url)}" target="_blank" rel="noopener" id="visual-open-tool"><button>Open ${esc(tool)}</button></a>`
    : '';
  const words = (m.expected_text || []).join('\\n');
  let body = '';

  if (m.state === 'skipped') {
    body = '<div class="sub">Removed. This lead is messaged with the page ' +
      'instead, not held waiting for a picture.</div>' +
      `<div class="row" style="margin-top:8px"><button onclick="skipViz('${id}', 'Approved')">Put it back</button></div>`;
  } else if (m.state === 'needs_image') {
    body = `
      <div class="viz-drop" tabindex="0" id="drop-${id}"
           onpaste="onVizPaste('${id}', event)"
           ondragover="event.preventDefault()"
           ondrop="onVizDrop('${id}', event)"
           onfocus="vizArm('${id}', true)" onblur="vizArm('${id}', false)"
           title="Click here, then press Ctrl+V. Or drop the file on this box.">
        Click here and press Ctrl+V to paste the picture, or drop the file here.
      </div>
      <div class="row" style="margin-top:8px">
        <input type="file" accept="image/png,image/jpeg,image/webp"
               onchange="onVizFile('${id}', this)" style="width:auto">
      </div>
      <div class="row" style="margin-top:8px">
        <input id="vizurl-${id}" placeholder="...or right-click the picture, Copy image address, paste here"
               oninput="vizArm('${id}', true)">
        <button onclick="attachVizUrl('${id}')">Fetch it</button>
      </div>
      <p class="hint" style="margin:6px 0 0">The link has to be the
        <b>picture's</b> own address, not the page it sits on &mdash;
        right-click the image and choose <b>Copy image address</b>. If the
        picture is only visible while you are signed in, this cannot reach it:
        save it and paste the file, which always works.</p>`;
  } else {
    const shot = `/api/screenshot/${id}`;
    const ready = m.state === 'ready';
    body = `
      <img class="viz-shot" src="${shot}" alt="">
      <div class="sub" style="margin-top:6px">Shown at about the size WhatsApp
        gives it in a chat. If it is not readable here, it is not readable there.</div>
      <div class="viz-proof">
        <div class="sub"><b>Proofread the picture</b> &mdash; the words below are
          what the prompt asked for. Check they appear on the image above and are
          spelled right. Nothing here can see the picture, so this is the one
          thing only you can do.</div>
        <div class="viz-words">${esc(words)}</div>
        <div class="hint" style="margin-top:6px">This is <b>not</b> the message.
          It is never sent anywhere &mdash; it is only here so you can compare it
          against the picture.</div>
      </div>
      <div class="row" style="margin-top:10px">
        ${ready
          ? '<span class="ok">Picture checked.</span>'
          : `<button onclick="confirmVizText('${id}')">The words are right</button>`}
        <button onclick="clearVizImage('${id}')">Wrong picture</button>
        <span class="sub" id="vizn-${id}">${esc(u.note || '')}</span>
      </div>
      ${ready ? vizMessage(m) : ''}`;
  }

  return `
      <div class="row" style="gap:10px;margin-top:8px">
        <span class="sub">${esc(m.style)} / ${esc(m.layout)}</span>
        ${m.specific ? '' : '<span class="sub viz-generic">not enough evidence to name them</span>'}
      </div>
      <details style="margin-top:8px">
        <summary class="sub">Why these cards &mdash; the evidence behind each one</summary>
        <div class="viz-words">${esc(m.gaps || 'No evidence recorded.')}</div>
      </details>
      <textarea id="vizp-${id}" readonly>${(m.prompt || '').replace(/</g, '&lt;')}</textarea>
      <div class="row" style="margin-top:10px">
        <button onclick="copyVizPrompt('${id}')">Copy prompt</button>
        ${openBtn}
        <button onclick="regenViz('${id}')">Different look</button>
        ${m.state === 'skipped' ? '' : `<button onclick="skipViz('${id}', 'Skip')">Remove</button>`}
        <span class="sub" id="vizc-${id}"></span>
      </div>
      ${body}`;
}

// --- who a run would write for -----------------------------------------
// Sixty prompts is not a batch anybody works through: each one is a visit to
// the image tool and a picture to read. A limit alone bounds the cost but
// picks whichever leads come first, which is not the same as picking the
// ones worth starting with.
let vizPicked = {};

async function loadVisualCandidates() {
  let d;
  try { d = await (await fetch('/api/visual/candidates')).json(); } catch { return; }
  const el = document.querySelector('#visual-groups');
  if (!el) return;
  if (!d.enabled) { el.innerHTML = ''; return; }
  if (d.error) { el.innerHTML = `<div class="long-err">${esc(d.error)}</div>`; return; }
  if (!d.total) {
    el.innerHTML = '<div class="sub">Every researched lead already has a prompt.</div>';
    return;
  }
  el.innerHTML = (d.groups || []).map(g => {
    if (!g.count) return '';
    const names = (g.leads || []).map(l => `
      <label class="sub viz-pick">
        <input type="checkbox" data-pick value="${esc(l.lead_id)}"
               ${vizPicked[l.lead_id] ? 'checked' : ''}
               onchange="vizPick('${esc(l.lead_id)}', this.checked)" style="width:auto">
        ${esc(l.name)}
      </label>`).join('');
    return `<details class="viz-group">
      <summary><b>${esc(g.label)}</b> &mdash; ${g.count}
        ${g.contactable
          ? `<button onclick="runVisualFor('${g.key}', event)">Write for this group</button>`
          : '<span class="sub">nothing can be written for these yet</span>'}
      </summary>
      <div class="viz-picks">${names}${g.capped ? '<span class="sub">...and more</span>' : ''}</div>
    </details>`;
  }).join('') +
    `<div class="row" style="margin-top:10px">
       <span class="sub" id="viz-picked-note"></span>
       <button onclick="runVisualFor('', event)" id="viz-run-picked">Write for the ones I ticked</button>
     </div>`;
  vizPickedNote();
}

function vizPick(leadId, on) {
  if (on) vizPicked[leadId] = true; else delete vizPicked[leadId];
  vizPickedNote();
}

function vizPickedNote() {
  const n = Object.keys(vizPicked).length;
  const note = document.querySelector('#viz-picked-note');
  if (note) note.textContent = n ? `${n} ticked` : 'Nothing ticked yet';
  const btn = document.querySelector('#viz-run-picked');
  if (btn) btn.disabled = !n;
}

async function runVisualFor(group, ev) {
  if (ev) { ev.preventDefault(); ev.stopPropagation(); }
  const howMany = document.querySelector('#viz-limit');
  const note = document.querySelector('#visual-note');
  const body = {
    groups: group ? [group] : [],
    lead_ids: group ? [] : Object.keys(vizPicked),
    limit: howMany && howMany.value ? howMany.value : null,
  };
  if (note) { note.textContent = 'Starting...'; note.className = 'hint'; }
  let d;
  try {
    d = await (await fetch('/api/visual/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    })).json();
  } catch (e) {
    if (note) { note.textContent = 'Could not reach the dashboard: ' + e; note.className = 'err'; }
    return;
  }
  if (!d.ok) {
    if (note) { note.textContent = d.error || 'Could not start.'; note.className = 'err'; }
    return;
  }
  if (note) { note.textContent = d.running + ' running...'; note.className = 'hint'; }
  vizPicked = {};
  vizPickedNote();
}

function repairTabsHtml() {
  return `<div class="row" style="margin-top:10px">
    <button onclick="repairTabs(this)">Add the missing tabs</button>
    <span class="hint" id="repair-note"></span>
  </div>`;
}

async function repairTabs(btn) {
  const note = document.querySelector('#repair-note');
  if (btn) btn.disabled = true;
  if (note) { note.textContent = 'Checking your Sheet...'; note.className = 'hint'; }
  let d;
  try {
    d = await (await fetch('/api/repair-tabs', {method: 'POST'})).json();
  } catch (e) {
    if (note) { note.textContent = 'Could not reach the dashboard: ' + e; note.className = 'err'; }
    if (btn) btn.disabled = false;
    return;
  }
  if (!d.ok) {
    if (note) { note.textContent = d.error || 'Could not repair the Sheet.'; note.className = 'err'; }
    if (btn) btn.disabled = false;
    return;
  }
  if (note) { note.textContent = d.note; note.className = 'ok'; }
  // Every panel that was reading the missing tab, not just this one.
  await loadVisual();
  refresh();
}

// What actually gets sent, shown on the card where the question is asked.
// The proofreading box above was read as the message -- a fair reading of
// "these exact words", and the kind of mistake you only make once a graphic
// has reached a real business.
function vizMessage(m) {
  if (m.sent_at) {
    // The draft stage will never touch this lead again -- a row on the
    // Outreach Log is what stops a business being written to twice. So the
    // only way its picture ever gets a message is this button.
    return `<div class="viz-sent">
      <div class="sub"><b>Already messaged on ${esc(m.sent_at)}</b>, with this:</div>
      <div class="viz-words">${esc(m.message || '')}</div>
      <p class="hint" style="margin:8px 0 0">That was the old message. Write a
        new one about this picture and the lead goes back into step 4's send
        list, with the graphic attached. The picture and the prompt are
        kept.</p>
      <div class="row" style="margin-top:8px">
        <button onclick="redraftViz('${encodeURIComponent(m.lead_id)}')">Write a new message for this picture</button>
        <span class="sub" id="vizr-${encodeURIComponent(m.lead_id)}"></span>
      </div>
    </div>`;
  }
  if (!m.message) {
    return `<div class="viz-next">
      <div class="sub"><b>Next: its message has not been written yet.</b></div>
      <p class="hint" style="margin:6px 0 0">Press <b>Draft messages</b> in
        step 4 and this business gets its own message &mdash; written from its
        own research, like the website ones. The picture above is attached to
        it. Then send it from step 4, the same queue as everything else.</p>
    </div>`;
  }
  // Whether step 4 is actually holding it, in step 4's own words. "It isn't
  // brought over" was the only report a person could make about a card that
  // said ready beside a queue that never offered the lead.
  // The one repair this dashboard has, offered where the problem is read
  // rather than only in step 4's excluded list -- which is a different
  // screen from the one saying the message is ready.
  const unblock = m.can_unblock
    ? `<button id="vizu-${esc(m.lead_id)}"
         onclick="unblockViz('${esc(m.lead_id)}')"
         style="margin-top:8px">Put it in step 4</button>`
    : '';
  const where = m.in_send_queue
    ? `<p class="hint" style="margin:6px 0 0">Waiting in
         <a href="#send-step">step 4</a> now, the same queue as the website
         messages. There is no separate send button here.</p>`
    : `<div class="long-err" style="margin:6px 0 0">Not in step 4 yet
         &mdash; ${esc(m.send_hold || 'step 4 is not offering this lead')}.
         ${unblock}</div>`;
  // Asked for by name. It was only ever offered on an already-sent lead,
  // where the old words are about a different pitch -- but a message you
  // simply do not like is the same problem with no button.
  const again = `<button class="ghost" onclick="redraftViz('${esc(m.lead_id)}')"
      style="margin-top:8px">Rewrite this message</button>
    <span class="sub" id="vizr-${esc(m.lead_id)}"></span>`;
  return `<div class="viz-next">
    <div class="sub"><b>The message this picture goes out with</b> &mdash;
      written for this business, and editable in step 4 before you send:</div>
    <div class="viz-words">${esc(m.message)}</div>
    ${where}
    <div class="row">${again}</div>
  </div>`;
}

async function unblockViz(leadId) {
  const btn = document.querySelector('#vizu-' + leadId);
  if (btn) { btn.disabled = true; btn.textContent = 'Putting it back...'; }
  let d;
  try {
    d = await (await fetch('/api/whatsapp/undo-send', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: decodeURIComponent(leadId)}),
    })).json();
  } catch (e) { d = {error: 'Could not reach the dashboard: ' + e}; }
  if (!d.ok) {
    if (btn) { btn.disabled = false; btn.textContent = 'Put it in step 4'; }
    vizSay(leadId, d.error || 'Could not do that.', true);
    return;
  }
  // Both panels change: this card stops saying it is held, and step 4
  // grows a row. Redrawing one and not the other is how a page ends up
  // disagreeing with itself.
  await loadVisual();
  if (typeof loadWhatsAppInline === 'function') await loadWhatsAppInline();
}

async function redraftViz(leadId) {
  const u = vizFor(leadId);
  const note = document.querySelector('#vizr-' + leadId);
  u.busy = true;
  if (note) { note.textContent = 'Writing a new message...'; note.className = 'sub'; }
  try {
    const r = await fetch('/api/visual/redraft', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: decodeURIComponent(leadId)})
    });
    const d = await r.json();
    if (!d.ok) {
      if (note) { note.textContent = d.error || 'Could not rewrite it.'; note.className = 'err'; }
      return;
    }
    await loadVisual();
    refresh();
  } catch (e) {
    if (note) { note.textContent = 'Could not reach the dashboard: ' + e; note.className = 'err'; }
  } finally { u.busy = false; }
}

function vizArm(leadId, on) {
  vizFor(leadId).armed = !!on;
}

function vizSay(leadId, text, bad) {
  const el = document.querySelector('#vizn-' + leadId) ||
             document.querySelector('#vizc-' + leadId);
  vizFor(leadId).note = text;
  if (el) { el.textContent = text; el.className = bad ? 'err' : 'sub'; }
}

async function copyVizPrompt(leadId) {
  const t = document.querySelector('#vizp-' + leadId);
  if (!t) return;
  t.select();
  try { await navigator.clipboard.writeText(t.value); }
  catch { document.execCommand('copy'); }
  const el = document.querySelector('#vizc-' + leadId);
  if (el) el.textContent = 'Copied.';
}

function vizReadFile(file, leadId) {
  // FileReader rather than fetch: the browser already has the bytes, and a
  // data URL avoids a multipart form -- which matters most on a phone, where
  // paste is the natural gesture and a file picker is not.
  const reader = new FileReader();
  reader.onload = () => attachVizImage(leadId, String(reader.result), '');
  reader.onerror = () => vizSay(leadId, 'Could not read that file.', true);
  reader.readAsDataURL(file);
}

function onVizPaste(leadId, event) {
  const items = (event.clipboardData && event.clipboardData.items) || [];
  for (const item of items) {
    if (item.type && item.type.indexOf('image/') === 0) {
      event.preventDefault();
      vizReadFile(item.getAsFile(), leadId);
      return;
    }
  }
  vizSay(leadId, 'That was not a picture. Copy the image itself, not the page.', true);
}

function onVizDrop(leadId, event) {
  event.preventDefault();
  const files = (event.dataTransfer && event.dataTransfer.files) || [];
  if (!files.length) { vizSay(leadId, 'Nothing was dropped.', true); return; }
  vizReadFile(files[0], leadId);
}

function onVizFile(leadId, input) {
  if (input.files && input.files.length) vizReadFile(input.files[0], leadId);
}

function attachVizUrl(leadId) {
  const el = document.querySelector('#vizurl-' + leadId);
  const url = el ? el.value.trim() : '';
  if (!url) { vizSay(leadId, 'Paste the link first.', true); return; }
  attachVizImage(leadId, '', url);
}

async function attachVizImage(leadId, dataUrl, url) {
  const u = vizFor(leadId);
  u.busy = true;
  vizSay(leadId, 'Saving the picture...');
  try {
    const r = await fetch('/api/visual/attach', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        lead_id: decodeURIComponent(leadId), data_url: dataUrl, url: url
      })
    });
    const d = await r.json();
    if (!d.ok) { vizSay(leadId, d.error || 'Could not save it.', true); return; }
    u.armed = false;
    u.note = '';
    await loadVisual();
  } catch (e) {
    vizSay(leadId, 'Could not reach the dashboard: ' + e, true);
  } finally {
    u.busy = false;
  }
}

async function confirmVizText(leadId) {
  const u = vizFor(leadId);
  u.busy = true;
  try {
    const r = await fetch('/api/visual/confirm', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: decodeURIComponent(leadId)})
    });
    const d = await r.json();
    if (!d.ok) { vizSay(leadId, d.error || 'Could not save that.', true); return; }
    u.note = '';
    await loadVisual();
    refresh();
  } finally { u.busy = false; }
}

async function clearVizImage(leadId) {
  const u = vizFor(leadId);
  u.busy = true;
  try {
    const r = await fetch('/api/visual/clear', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: decodeURIComponent(leadId)})
    });
    const d = await r.json();
    if (!d.ok) { vizSay(leadId, d.error || 'Could not clear it.', true); return; }
    u.note = '';
    await loadVisual();
  } finally { u.busy = false; }
}

async function skipViz(leadId, decision) {
  const u = vizFor(leadId);
  u.busy = true;
  try {
    const r = await fetch('/api/visual/skip', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: decodeURIComponent(leadId), decision: decision})
    });
    const d = await r.json();
    if (!d.ok) { vizSay(leadId, d.error || 'Could not save that.', true); return; }
    await loadVisual();
  } finally { u.busy = false; }
}

async function regenViz(leadId) {
  const u = vizFor(leadId);
  u.busy = true;
  vizSay(leadId, 'Writing a new prompt...');
  try {
    const r = await fetch('/api/visual/regenerate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lead_id: decodeURIComponent(leadId)})
    });
    const d = await r.json();
    if (!d.ok) { vizSay(leadId, d.error || 'Could not rewrite it.', true); return; }
    u.note = '';
    await loadVisual();
  } finally { u.busy = false; }
}

// The track selector sits OUTSIDE the Infographic step, which is hidden when
// the campaign pitches a page -- putting it inside would mean the one control
// that switches graphics on was only visible once they were already on.
function trackPicked() {
  const pick = document.querySelector('#track-pick');
  const needs = pick && (pick.value === 'visual' || pick.value === 'both');
  const wrap = document.querySelector('#sig-wrap');
  const help = document.querySelector('#sig-help');
  const tool = document.querySelector('#tool-wrap');
  if (wrap) wrap.style.display = needs ? 'block' : 'none';
  if (help) help.style.display = needs ? 'block' : 'none';
  if (tool) tool.style.display = needs ? 'block' : 'none';
}

async function loadTrack() {
  let d;
  try { d = await (await fetch('/api/track')).json(); } catch { return; }
  if (d.error) return;
  const pick = document.querySelector('#track-pick');
  const sig = document.querySelector('#track-sig');
  const name = document.querySelector('#tool-name');
  const url = document.querySelector('#tool-url');
  if (pick) pick.value = d.track || 'website';
  if (sig) sig.value = d.signature || '';
  if (name) name.value = d.tool_name || '';
  // Prefilled with the suggestion only while nothing is saved, so what is in
  // the box is what pressing Save will store -- and so a cleared address
  // stays cleared rather than coming back on the next load.
  if (url) url.value = d.tool_url || d.tool_url_suggestion || '';
  trackPicked();
}

async function saveTrack() {
  const pick = document.querySelector('#track-pick');
  const sig = document.querySelector('#track-sig');
  const note = document.querySelector('#track-note');
  if (note) { note.textContent = 'Saving...'; note.className = 'hint'; }
  const r = await fetch('/api/track', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      track: pick ? pick.value : 'website',
      signature: sig ? sig.value : '',
      tool_name: (document.querySelector('#tool-name') || {}).value,
      tool_url: (document.querySelector('#tool-url') || {}).value,
    })
  });
  const d = await r.json();
  if (note) {
    note.textContent = d.ok ? 'Saved.' : (d.error || 'Could not save that.');
    note.className = d.ok ? 'ok' : 'err';
  }
  if (d.ok) { await loadVisual(); refresh(); }
}

// Tick boxes for the rotation. Drawn from what the server says exists rather
// than a list written out here: a name typed twice drifts, and the server
// already refuses one it does not know.
function drawRotation(cat) {
  const boxes = (id, all, chosen) => {
    const el = document.querySelector(id);
    if (!el) return;
    el.innerHTML = (all || []).map(name => `
      <label class="sub" style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" data-rot="${id}" value="${esc(name)}"
               ${(chosen || []).indexOf(name) !== -1 ? 'checked' : ''}
               style="width:auto">
        ${esc(name)}
      </label>`).join('');
  };
  boxes('#rot-styles', cat.styles, cat.chosen_styles);
  boxes('#rot-layouts', cat.layouts, cat.chosen_layouts);
}

function rotationPicked(id) {
  return [...document.querySelectorAll(`input[data-rot="${id}"]`)]
    .filter(b => b.checked).map(b => b.value);
}

async function saveRotation() {
  const note = document.querySelector('#rot-note');
  if (note) { note.textContent = 'Saving...'; note.className = 'hint'; }
  const r = await fetch('/api/visual/rotation', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      styles: rotationPicked('#rot-styles'),
      layouts: rotationPicked('#rot-layouts'),
    })
  });
  const d = await r.json();
  if (note) {
    note.textContent = d.ok
      ? 'Saved. New prompts use it; press Different look to re-roll one you already have.'
      : (d.error || 'Could not save that.');
    note.className = d.ok ? 'ok' : 'err';
  }
  if (d.ok) await loadVisual();
}

refresh();
loadTarget();
loadModel();
loadNextUp();
loadReview();
loadQueue();
loadReach();
loadDesignLevel();
loadServices();
loadVisual();
loadTrack();
poll();
setInterval(poll, 1200);
setInterval(refresh, 30000);
setInterval(loadQueue, 30000);
// Keeps the position you are at, so a background refresh never yanks the card
// out from under a decision you are part-way through making.
setInterval(() => loadReview(true), 30000);
// Stands aside while a card is mid-decision -- a link half typed or a
// confirmation being read must not be wiped by a background load.
setInterval(() => { if (!vizBusy()) loadVisual(); }, 30000);
</script>
</body>
</html>
"""
