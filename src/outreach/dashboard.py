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
from dataclasses import dataclass, field

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
    "site-build": ["run", "site-build"],
    "outreach-draft": ["run", "outreach-draft"],
    "preflight": ["preflight"],
    "preview-leads": ["run", "discovery", "--dry-run"],
    "preview-messages": ["run", "outreach-draft", "--dry-run"],
}

FULL_RUN = ["discovery", "research", "site-brief", "outreach-draft"]


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
) -> Summary:
    """Turn raw Sheet rows into the numbers a founder acts on.

    Pure and side-effect free, so it can be tested without a Sheet.
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
        if (
            lead_id not in outreach
            and rank(Stage.RESEARCHED) <= rank(current) < rank(Stage.EMAIL_DRAFTED)
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


class Runner:
    """Runs one stage at a time in a subprocess, streaming output to the page."""

    def __init__(self) -> None:
        self.lines: deque[str] = deque(maxlen=500)
        self.running: str | None = None
        self._lock = threading.Lock()

    def start(self, label: str, stages: list[str]) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = label
            self.lines.clear()
        threading.Thread(target=self._run, args=(stages,), daemon=True).start()
        return True

    def _run(self, stages: list[str]) -> None:
        try:
            for stage in stages:
                args = STAGE_COMMANDS[stage]
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


def _load_summary(config: Config) -> Summary:
    from .auth import get_credentials
    from .sheets import SheetsClient

    if not config.secrets.sheet_id:
        return Summary()

    creds = get_credentials(config.secrets.oauth_client_file, config.secrets.token_file)
    sheets = SheetsClient(creds, config.secrets.sheet_id)

    summary = summarise(
        sheets.read_rows(schema.LEADS),
        set(sheets.read_rows(schema.RESEARCH)),
        sheets.read_rows(schema.SITE_BRIEFS),
        sheets.read_rows(schema.OUTREACH_LOG),
        sheets.read_rows(schema.ERRORS),
    )
    summary.sheet_url = sheets.url
    return summary


def create_app(config: Config) -> Flask:
    app = Flask(__name__)
    runner = Runner()

    @app.route("/")
    def index():
        return render_template_string(PAGE, campaign=config.campaign)

    @app.route("/api/summary")
    def api_summary():
        try:
            summary = _load_summary(config)
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

    @app.route("/api/run", methods=["POST"])
    def api_run():
        what = (request.json or {}).get("what", "")
        if what == "full":
            started = runner.start("Full pipeline", FULL_RUN)
        elif what in STAGE_COMMANDS:
            started = runner.start(what, [what])
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
  .status { font-size:13px; color:var(--warn); }
  .err { color:var(--accent); }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>SME Outreach</h1>
    <div class="sub">{{ campaign.category }} &middot; {{ campaign.location }}</div>
  </header>
  <div class="sub" id="totals">Loading&hellip;</div>

  <h2>What needs you today</h2>
  <div class="todo" id="todo"></div>

  <h2>Pipeline</h2>
  <div class="funnel" id="funnel"></div>

  <h2>Run something</h2>
  <div class="card">
    <div class="row">
      <button class="primary" data-run="full">Run everything</button>
      <button data-run="discovery">Find leads</button>
      <button data-run="research">Research</button>
      <button data-run="site-brief">Site prompts</button>
      <button data-run="site-build">Build approved sites</button>
      <button data-run="outreach-draft">Draft messages</button>
    </div>
    <p class="hint">
      Running anything twice is free &mdash; every stage skips what it already did.
    </p>
    <div class="row" style="margin-top:12px">
      <button data-run="preview-leads">Preview leads</button>
      <button data-run="preview-messages">Preview messages</button>
      <button data-run="preflight">Check setup</button>
      <span class="status" id="running"></span>
    </div>
  </div>

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
    $('#totals').innerHTML = `<span class="err">${d.error}</span>`;
    return;
  }
  $('#totals').innerHTML = d.total
    ? `${d.total} leads &middot; ${d.disqualified} disqualified &middot;
       <a href="${d.sheet_url}" target="_blank" rel="noopener">Open Sheet</a>`
    : 'No leads yet &mdash; press <b>Find leads</b> to start.';

  const todo = [];
  if (d.emails_waiting) todo.push(card(d.emails_waiting, 'emails to send',
    'Open Gmail drafts, read, press send'));
  if (d.whatsapp_waiting) todo.push(card(d.whatsapp_waiting, 'WhatsApp to send',
    'Copy from the Outreach Log tab'));
  if (d.to_research) todo.push(card(d.to_research, 'leads to research',
    'About 6&cent; each'));
  if (d.to_brief) todo.push(card(d.to_brief, 'need site prompts', 'Free to generate'));
  if (d.awaiting_approval) todo.push(card(d.awaiting_approval, 'prompts awaiting you',
    'Set Approval Status to Approved in the Site Briefs tab'));
  if (d.approved_unbuilt) todo.push(card(d.approved_unbuilt, 'approved, not built',
    'Press Build approved sites'));
  if (d.to_draft) todo.push(card(d.to_draft, 'need messages drafted', 'Nothing sends'));
  if (d.errors) todo.push(card(d.errors, 'errors logged',
    `${d.errors_retryable} may fix themselves on a re-run`));
  $('#todo').innerHTML = todo.length ? todo.join('')
    : '<div class="card"><div class="what">Nothing waiting.</div>' +
      '<div class="sub">Find new leads, or change the category in your campaign file.</div></div>';

  $('#funnel').innerHTML = Object.entries(d.by_stage).map(([k, v]) =>
    `<div class="step ${v ? '' : 'empty'}"><div class="c">${v}</div>
     <div class="l">${k}</div></div>`).join('');
}

async function poll() {
  let d;
  try { d = await (await fetch('/api/log')).json(); } catch { return; }
  if (d.lines.length) $('#log').textContent = d.lines.join('\\n');
  $('#log').scrollTop = $('#log').scrollHeight;
  const busy = !!d.running;
  $('#running').textContent = busy ? `Running ${d.running}\\u2026` : '';
  document.querySelectorAll('button[data-run]').forEach(b => b.disabled = busy);
  if (!busy && poll.wasBusy) refresh();
  poll.wasBusy = busy;
}

document.querySelectorAll('button[data-run]').forEach(b =>
  b.addEventListener('click', async () => {
    await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({what: b.dataset.run})
    });
    poll();
  }));

refresh();
poll();
setInterval(poll, 1200);
setInterval(refresh, 30000);
</script>
</body>
</html>
"""
