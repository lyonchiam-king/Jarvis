"""The dashboard's numbers, and that it stays local and read-only."""

from dataclasses import replace
import ast
import re
from pathlib import Path

import pytest

from outreach.config import load_config
from outreach.dashboard import (
    FULL_RUN,
    STAGE_COMMANDS,
    create_app,
    review_queue,
    summarise,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "outreach"


def lead(lead_id, stage):
    return {"Lead ID": lead_id, "Stage": stage}


def index(rows):
    return {r["Lead ID"]: (i + 2, r) for i, r in enumerate(rows)}


def live(local: Path) -> Path:
    """Whichever campaign file is in force: the local fork, else the template.

    A rejected edit never forks, so the template is still the live one.
    """
    return local if local.exists() else local.with_name(
        local.name.replace(".local.yaml", ".yaml")
    )


# ------------------------------------------------------------- the numbers


def test_counts_leads_by_stage_and_excludes_disqualified_from_the_funnel():
    leads = index(
        [
            lead("a", "Discovered"),
            lead("b", "Researched"),
            lead("c", "Sent"),
            lead("d", "Disqualified"),
        ]
    )
    s = summarise(leads, set(), set(), {}, {})

    assert s.total == 4
    assert s.disqualified == 1
    assert s.by_stage["Discovered"] == 1
    assert s.by_stage["Sent"] == 1
    assert sum(s.by_stage.values()) == 3, "disqualified must not appear in the funnel"


def test_outstanding_work_matches_what_the_stages_would_actually_do():
    leads = index(
        [
            lead("fresh", "Discovered"),
            lead("done_research", "Researched"),
            lead("drafted", "Email Drafted"),
        ]
    )
    s = summarise(leads, {"done_research", "drafted"}, {"drafted"}, {"drafted": (2, {})}, {})

    assert s.to_research == 1      # only `fresh`
    assert s.to_brief == 1         # only `done_research`
    assert s.to_draft == 1         # only `done_research`


def test_a_disqualified_lead_is_never_counted_as_outstanding_work():
    s = summarise(index([lead("x", "Disqualified")]), set(), set(), {}, {})
    assert (s.to_research, s.to_brief, s.to_draft) == (0, 0, 0)


def test_blank_stage_counts_as_discovered():
    s = summarise(index([lead("x", "")]), set(), set(), {}, {})
    assert s.by_stage["Discovered"] == 1
    assert s.to_research == 1


def test_splits_waiting_messages_by_channel():
    outreach = {
        "a": (2, {"Channel": "Email", "Status": "Drafted"}),
        "b": (3, {"Channel": "WhatsApp", "Status": "Drafted"}),
        "c": (4, {"Channel": "Email", "Status": "Sent"}),
    }
    s = summarise({}, set(), set(), outreach, {})

    assert s.emails_waiting == 1
    assert s.whatsapp_waiting == 1, "already-sent rows must not be counted as waiting"


def test_unreachable_rows_are_not_counted_as_waiting():
    outreach = {
        "a": (2, {"Channel": "Unreachable", "Status": "Drafted"}),
        "b": (3, {"Channel": "WhatsApp", "Status": "Drafted"}),
    }
    s = summarise({}, set(), set(), outreach, {})

    assert s.emails_waiting == 0
    assert s.whatsapp_waiting == 1, "unreachable leads have nothing to send"


def test_counts_retryable_errors_separately():
    errors = {
        "1": (2, {"Retryable": "TRUE"}),
        "2": (3, {"Retryable": "FALSE"}),
    }
    s = summarise({}, set(), set(), {}, errors)
    assert (s.errors, s.errors_retryable) == (2, 1)


def test_has_work_is_false_on_a_finished_pipeline():
    leads = index([lead("a", "Closed")])
    assert summarise(leads, {"a"}, {"a"}, {"a": (2, {"Status": "Sent"})}, {}).has_work is False


# ---------------------------------------------------------------- the app


@pytest.fixture
def client():
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    return create_app(config).test_client()


def test_page_renders_with_the_campaign_on_it(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"hair salon" in response.data
    assert b"Georgetown" in response.data


def test_unknown_action_is_rejected(client):
    assert client.post("/api/run", json={"what": "rm -rf"}).status_code == 400


def test_log_endpoint_reports_idle_before_anything_runs(client):
    assert client.get("/api/log").get_json() == {"running": None, "lines": []}


def test_every_button_maps_to_a_real_command():
    from outreach.cli import main

    run_group = main.commands["run"]
    for name, args in STAGE_COMMANDS.items():
        top = args[0]
        assert top in main.commands, f"{name} -> unknown command {top}"
        if top == "run":
            assert args[1] in run_group.commands, f"{name} -> unknown stage {args[1]}"


def test_full_run_covers_the_pipeline_in_order():
    assert FULL_RUN == ["discovery", "research", "site-brief", "outreach-draft"]


# ---------------------------------------------------------- safety checks


def test_dashboard_binds_to_localhost_only():
    """It must never be reachable from the network -- it can spend money."""
    source = (SRC / "dashboard.py").read_text()
    tree = ast.parse(source)
    hosts = [
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        for kw in node.keywords
        if kw.arg == "host" and isinstance(kw.value, ast.Constant)
    ]
    assert hosts == ["127.0.0.1"], f"expected localhost only, found {hosts}"
    assert "0.0.0.0" not in source


def test_dashboard_never_shells_out_to_a_string_command():
    """Subprocess calls must pass a list, never shell=True."""
    tree = ast.parse((SRC / "dashboard.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("Popen", "run", "call", "check_output"):
                for kw in node.keywords:
                    assert not (
                        kw.arg == "shell"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value
                    ), "shell=True in the dashboard"


# ------------------------------------------- site briefs and Lovable gating


def brief(lead_id, approval="Pending", project_id=""):
    return {
        "Lead ID": lead_id,
        "Approval Status": approval,
        "Lovable Project ID": project_id,
    }


def test_counts_prompts_waiting_on_your_approval():
    briefs = index([brief("a"), brief("b", approval=""), brief("c", approval="Approved")])
    s = summarise({}, set(), briefs, {}, {})

    assert s.awaiting_approval == 2, "blank and Pending both mean 'waiting on you'"
    assert s.approved_unbuilt == 1


def test_an_already_built_site_is_not_outstanding_work():
    briefs = index([brief("a", approval="Approved", project_id="proj_1")])
    s = summarise({}, set(), briefs, {}, {})

    assert s.approved_unbuilt == 0
    assert s.awaiting_approval == 0


def test_a_skipped_brief_is_not_counted_either_way():
    s = summarise({}, set(), index([brief("a", approval="Skip")]), {}, {})
    assert (s.awaiting_approval, s.approved_unbuilt) == (0, 0)


def test_summarise_still_accepts_a_bare_set_of_ids():
    """Callers that only have ids shouldn't have to fabricate rows."""
    leads = index([lead("a", "Researched")])
    s = summarise(leads, {"a"}, {"a"}, {}, {})
    assert s.to_brief == 0


def test_site_build_is_reachable_from_the_dashboard():
    assert "site-build" in STAGE_COMMANDS


# ------------------------------------------------ demo-first: require_site


def test_require_site_does_not_count_a_siteless_lead_as_ready_to_draft():
    """Otherwise the card sends you to press a button that drafts nothing."""
    leads = index([lead("a", "Site Briefed")])

    assert summarise(leads, {"a"}, {"a"}, {}, {}).to_draft == 1
    assert summarise(leads, {"a"}, {"a"}, {}, {}, require_site=True).to_draft == 0


def test_require_site_counts_the_lead_once_its_demo_is_built():
    leads = index([lead("a", "Site Built")])
    s = summarise(leads, {"a"}, {"a"}, {}, {}, require_site=True)
    assert s.to_draft == 1


def test_full_run_does_not_include_site_build():
    """Building spends Lovable credits -- it must never ride along with 'run everything'."""
    assert "site-build" not in FULL_RUN


# ------------------------------------------------- the manual build loop


def test_record_build_requires_a_lead_id(client):
    assert client.post("/api/record-build", json={}).status_code == 400


def test_record_build_rejects_a_non_http_url(client):
    """Guards against a javascript: or data: URL landing in the Sheet."""
    r = client.post(
        "/api/record-build", json={"lead_id": "a", "preview_url": "javascript:alert(1)"}
    )
    assert r.status_code == 400
    assert "http" in r.get_json()["error"]


def test_build_queue_is_empty_without_a_sheet(client):
    assert client.get("/api/build-queue").get_json() == {"queue": []}


def test_page_offers_the_copy_and_open_builder_flow(client):
    body = client.get("/").data
    assert b"Copy prompt" in body
    # The "Open [tool]" button target now comes from config, not a hardcoded
    # Lovable URL -- the default config pins Google AI Studio.
    assert b"aistudio.google.com" in body
    assert b"Mark as built" in body


def _client_with_build(**build_overrides):
    """A test client whose build settings are patched, without touching the
    live campaign file. Same replace() pattern as the config tests."""
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, build=replace(config.build, **build_overrides))
    return create_app(config).test_client()


def test_build_panel_uses_config_site_builder():
    """The Build panel labels the paste target from config, not a hardcoded
    string -- so the h2 subtitle and the queue-card button both read the
    campaign's site builder."""
    client = _client_with_build(
        site_builder_name="Acme Builder", site_builder_url="https://acme.example/"
    )
    body = client.get("/").data.decode()
    # h2 subtitle is server-rendered HTML.
    assert "paste into Acme Builder" in body
    # The const the queue-card button reads from, server-rendered by Jinja.
    assert 'const SITE_BUILDER = "Acme Builder"' in body
    assert 'const SITE_BUILDER_URL = "https://acme.example/"' in body


def test_build_panel_defaults_render_google_ai_studio():
    """The untouched campaign shows Google AI Studio -- pins the default-render
    contract so a silent default change is caught."""
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    client = create_app(config).test_client()
    body = client.get("/").data.decode()
    assert "paste into Google AI Studio" in body
    assert 'const SITE_BUILDER = "Google AI Studio"' in body
    assert 'const SITE_BUILDER_URL = "https://aistudio.google.com/"' in body


def test_build_panel_open_button_when_url_blank():
    """A blank URL must not render a dead link. We chose to omit the anchor and
    render a bare button -- assert that choice so the edge stays handled."""
    client = _client_with_build(site_builder_name="Acme Builder", site_builder_url="")
    body = client.get("/").data.decode()
    assert 'const SITE_BUILDER_URL = ""' in body
    # The queue-card template literal renders a bare button when the URL is
    # blank -- no anchor wrapping it, and never a placeholder href.
    assert "`<button>Open ${SITE_BUILDER}</button>`" in body
    assert "href=\"#\"" not in body


def test_page_lays_the_morning_out_in_order(client):
    """Gather, review, build, send -- the order you actually work in."""
    body = client.get("/").data.decode()
    headings = [f"</span> {s}" for s in ("Gather", "Review", "Build", "Send")]
    for heading in headings:
        assert heading in body, f"missing step heading: {heading}"
    positions = [body.index(h) for h in headings]
    assert positions == sorted(positions)


def test_the_review_step_offers_approve_and_skip(client):
    body = client.get("/").data
    assert b"Approve" in body
    assert b"Skip" in body
    assert b"Nothing is spent until you" in body


# ------------------------------------------- the MCP hooks Claude Code uses


def test_briefs_commands_exist_for_an_mcp_client():
    """Claude Code drives Lovable through these two commands."""
    from outreach.cli import main

    assert "briefs" in main.commands
    briefs = main.commands["briefs"]
    assert "pending" in briefs.commands
    assert "record" in briefs.commands


def test_record_rejects_a_non_http_preview_url_from_the_cli():
    from click.testing import CliRunner

    from outreach.cli import main

    result = CliRunner().invoke(
        main, ["briefs", "record", "--lead-id", "a", "--preview-url", "javascript:x"]
    )
    assert result.exit_code != 0
    assert "http" in result.output


def test_build_sites_skill_exists_and_forbids_prompt_editing():
    """The skill is the instructions Claude Code follows -- guard its rules."""
    skill = (
        Path(__file__).resolve().parent.parent
        / ".claude/skills/build-sites/SKILL.md"
    )
    assert skill.exists()
    text = skill.read_text()
    assert "outreach briefs pending --json" in text
    assert "outreach briefs record" in text
    assert "Do not improve, shorten, or reword the prompt" in text
    assert "Record after each build" in text


# ------------------------------------------------ step 2: the review queue


def brief_row(lead_id, approval="Pending", project_id="", prompt="Build a site"):
    return {
        "Lead ID": lead_id,
        "Business Name": f"Salon {lead_id}",
        "Lovable Prompt": prompt,
        "Approval Status": approval,
        "Lovable Project ID": project_id,
    }


def lead_row(lead_id, **extra):
    return {
        "Lead ID": lead_id,
        "Stage": "Site Briefed",
        "Business Name": f"Salon {lead_id}",
        "Category": "Hair salon",
        "Phone": "04-262 1188",
        "Rating": "4.7",
        "Review Count": "213",
        **extra,
    }


def research_row(lead_id, **extra):
    return {
        "Lead ID": lead_id,
        "Suggested Website Angle": "They get booking questions in the reviews",
        "Customer Praise": "Friendly and fast",
        "Customer Pain Points": "No way to book",
        **extra,
    }


def test_only_undecided_briefs_reach_the_review_queue():
    briefs = index(
        [
            brief_row("pending"),
            brief_row("blank", approval=""),
            brief_row("yes", approval="Approved"),
            brief_row("no", approval="Skip"),
        ]
    )
    queue = review_queue({}, {}, briefs)

    assert {q["lead_id"] for q in queue} == {"pending", "blank"}


def test_an_already_built_brief_is_not_re_reviewed():
    briefs = index([brief_row("a", approval="Pending", project_id="proj_1")])
    assert review_queue({}, {}, briefs) == []


def test_the_review_card_carries_enough_to_decide_without_the_sheet():
    """Deciding blind is how you approve sixty sites you never looked at."""
    queue = review_queue(
        index([lead_row("a")]),
        index([research_row("a", **{"Social Handle": "@salon_a"})]),
        index([brief_row("a", prompt="Build a booking page")]),
    )

    assert len(queue) == 1
    q = queue[0]
    assert q["name"] == "Salon a"
    assert q["rating"] == "4.7" and q["reviews"] == "213"
    assert "booking questions" in q["angle"]
    assert q["praise"] == "Friendly and fast"
    assert q["social"] == "@salon_a"
    assert q["prompt"] == "Build a booking page"


def test_a_brief_with_no_research_row_still_renders():
    """A missing row must not blank the whole queue."""
    queue = review_queue({}, {}, index([brief_row("a")]))
    assert queue[0]["name"] == "Salon a"
    assert queue[0]["angle"] == ""


def test_decide_rejects_anything_that_is_not_approve_or_skip(client):
    r = client.post("/api/decide", json={"lead_id": "a", "decision": "build it now"})
    assert r.status_code == 400


def test_decide_requires_a_lead(client):
    assert client.post("/api/decide", json={"decision": "approved"}).status_code == 400


def test_approving_writes_only_the_approval_cell(monkeypatch):
    """Approving must not build, and must not touch any other column."""
    writes = []

    class FakeSheets:
        url = "https://sheet"

        def update_column(self, tab, column, values, *, user_edit=False):
            from outreach.sheets import guard_manual_column

            guard_manual_column(tab, column, user_edit=user_edit)
            writes.append((tab.title, column, dict(values)))
            return len(values)

    from outreach import dashboard as dash

    monkeypatch.setattr(dash, "_sheets_client", lambda config: FakeSheets())
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    client = create_app(config).test_client()

    r = client.post("/api/decide", json={"lead_id": "a", "decision": "approved"})

    assert r.get_json() == {"ok": True}
    assert writes == [("Site Briefs", "Approval Status", {"a": "Approved"})]


def test_skipping_writes_skip(monkeypatch):
    writes = []

    class FakeSheets:
        def update_column(self, tab, column, values, *, user_edit=False):
            from outreach.sheets import guard_manual_column

            guard_manual_column(tab, column, user_edit=user_edit)
            writes.append(dict(values))
            return len(values)

    from outreach import dashboard as dash

    monkeypatch.setattr(dash, "_sheets_client", lambda config: FakeSheets())
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    client = create_app(config).test_client()
    client.post("/api/decide", json={"lead_id": "a", "decision": "skip"})

    assert writes == [{"a": "Skip"}]


def test_deciding_on_a_missing_row_says_so_rather_than_claiming_success(monkeypatch):
    class FakeSheets:
        def update_column(self, tab, column, values, *, user_edit=False):
            from outreach.sheets import guard_manual_column

            guard_manual_column(tab, column, user_edit=user_edit)
            return 0

    from outreach import dashboard as dash

    monkeypatch.setattr(dash, "_sheets_client", lambda config: FakeSheets())
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    client = create_app(config).test_client()

    body = client.post("/api/decide", json={"lead_id": "ghost", "decision": "approved"})
    assert "ghost" in body.get_json()["error"]


# ------------------------------------------- choosing what to hunt, from the page


@pytest.fixture
def campaign_app(tmp_path, monkeypatch):
    """An app pointed at a throwaway copy of the real campaign file.

    Returns the *live* path -- edits fork to a gitignored `.local.yaml` so a
    `git pull` never collides with your settings, so that is where to look.
    """
    from pathlib import Path

    from outreach.campaign_edit import local_path_for

    src = Path("config/campaigns/penang-my.yaml")
    path = tmp_path / "campaign.yaml"
    path.write_text(src.read_text())
    monkeypatch.chdir(tmp_path)
    config = load_config(path, env_file=None)
    return create_app(config).test_client(), local_path_for(path)


def test_the_page_shows_what_you_are_hunting(client):
    body = client.get("/").data
    assert b"What you're hunting" in body or b"What you&#39;re hunting" in body
    assert b"Change" in body


def test_campaign_endpoint_reports_the_current_target(campaign_app):
    client, _path = campaign_app
    d = client.get("/api/campaign").get_json()

    assert d["category"] == "hair salon"
    assert len(d["areas"]) == 18
    assert d["batch_size"] == 60


def test_changing_the_category_is_written_to_the_file(campaign_app):
    client, path = campaign_app
    d = client.post("/api/campaign", json={"category": "barber shop"}).get_json()

    assert d["ok"] is True
    assert d["category"] == "barber shop"
    assert 'category: "barber shop"' in path.read_text()


def test_a_change_takes_effect_without_a_restart(campaign_app):
    """The whole point of editing from the page."""
    client, _path = campaign_app
    client.post("/api/campaign", json={"category": "cafe"})

    assert client.get("/api/campaign").get_json()["category"] == "cafe"
    assert b"cafe" in client.get("/").data


def test_areas_can_be_replaced_from_the_page(campaign_app):
    client, path = campaign_app
    d = client.post(
        "/api/campaign", json={"areas": ["Ipoh, Perak", "Taiping, Perak"]}
    ).get_json()

    assert d["areas"] == ["Ipoh, Perak", "Taiping, Perak"]
    assert "Bayan Lepas" not in path.read_text()


def test_saving_keeps_the_files_comments(campaign_app):
    client, path = campaign_app
    before = live(path).read_text().count("#")
    client.post("/api/campaign", json={"category": "cafe", "areas": ["Ipoh"]})

    assert path.read_text().count("#") == before


def test_an_empty_category_is_refused_and_nothing_is_written(campaign_app):
    client, path = campaign_app
    client.post("/api/campaign", json={"category": "cafe"})  # force the fork
    before = path.read_text()

    d = client.post("/api/campaign", json={"category": "  "}).get_json()

    assert "cannot be empty" in d["error"]
    assert path.read_text() == before, "a rejected edit must not touch the file"


def test_duplicate_areas_are_refused(campaign_app):
    client, _path = campaign_app
    d = client.post("/api/campaign", json={"areas": ["Ipoh", "Ipoh"]}).get_json()
    assert "Duplicate area" in d["error"]


def test_areas_must_be_a_list(campaign_app):
    client, _path = campaign_app
    r = client.post("/api/campaign", json={"areas": "Ipoh"})
    assert r.status_code == 400


def test_a_rejected_edit_leaves_a_loadable_campaign(campaign_app):
    """However the edit fails, the pipeline must still start tomorrow."""
    client, path = campaign_app
    client.post("/api/campaign", json={"category": ""})
    client.post("/api/campaign", json={"location": ""})

    cfg = load_config(live(path), env_file=None)
    assert cfg.campaign.category == "hair salon"
    assert cfg.campaign.areas


def test_the_new_target_is_what_the_next_run_would_search(campaign_app):
    client, path = campaign_app
    client.post(
        "/api/campaign",
        json={"category": "dental clinic", "areas": ["Ipoh, Perak", "Taiping, Perak"]},
    )

    cfg = load_config(path, env_file=None)
    assert cfg.campaign.search_queries == (
        "dental clinic in Ipoh, Perak",
        "dental clinic in Taiping, Perak",
    )


# ------------------------------------------------ the handbook, and first run


def test_the_handbook_renders_in_the_app(client):
    r = client.get("/guide")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Running this every day" in body
    assert "Back to the dashboard" in body


def test_the_dashboard_links_to_the_handbook(client):
    assert b'href="/guide"' in client.get("/").data


def test_each_step_links_to_its_place_in_the_handbook(client):
    """A help link that 404s inside the page is worse than none."""
    from outreach.guide import guide_html

    page = client.get("/").data.decode()
    rendered = guide_html()

    anchors = re.findall(r'href="/guide#([^"]+)"', page)
    assert anchors, "no contextual help links on the page"
    for anchor in set(anchors):
        assert f'id="{anchor}"' in rendered, f"help link points nowhere: #{anchor}"


def test_an_unconfigured_install_says_what_is_missing(monkeypatch, tmp_path):
    """Otherwise the page invites you to press a button that will just fail."""
    from dataclasses import replace

    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, sheet_id="", places_api_key=""))
    client = create_app(config).test_client()

    d = client.get("/api/summary").get_json()

    assert "SHEET_ID" in d["setup_needed"]
    assert "PLACES_API_KEY" in d["setup_needed"]
    assert "total" not in d


def test_a_configured_install_reports_numbers_not_a_setup_prompt(monkeypatch):
    from dataclasses import replace

    from outreach import dashboard as dash

    monkeypatch.setattr(dash, "_load_summary", lambda config: dash.Summary(total=7))
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(
        config,
        secrets=replace(
            config.secrets, sheet_id="s", places_api_key="p", anthropic_api_key="a"
        ),
    )
    client = create_app(config).test_client()

    d = client.get("/api/summary").get_json()
    assert d["total"] == 7
    assert "setup_needed" not in d


# --------------------------------------------------- "how many" per run


def test_stage_args_appends_the_limit():
    from outreach.dashboard import stage_args

    assert stage_args("research", 5) == ["run", "research", "--limit", "5"]
    assert stage_args("research") == ["run", "research"]


def test_a_dry_run_still_takes_a_limit():
    from outreach.dashboard import stage_args

    assert stage_args("preview-leads", 3) == [
        "run", "discovery", "--dry-run", "--limit", "3",
    ]


def test_preflight_never_gets_a_limit():
    """It checks connections; a number would be meaningless and would error."""
    from outreach.dashboard import stage_args

    assert stage_args("preflight", 10) == ["preflight"]


def test_every_stage_that_takes_a_limit_really_accepts_one():
    """Guards against offering a number to a command that would reject it."""
    from outreach.cli import main
    from outreach.dashboard import STAGE_COMMANDS, TAKES_LIMIT

    run_group = main.commands["run"]
    for stage in TAKES_LIMIT:
        args = STAGE_COMMANDS[stage]
        command = run_group.commands[args[1]] if args[0] == "run" else main.commands[args[0]]
        options = {opt for param in command.params for opt in param.opts}
        assert "--limit" in options, f"{stage} is offered a limit it cannot take"


def test_no_stage_outside_the_set_is_given_a_limit():
    from outreach.cli import main
    from outreach.dashboard import STAGE_COMMANDS, TAKES_LIMIT

    run_group = main.commands["run"]
    for stage, args in STAGE_COMMANDS.items():
        if stage in TAKES_LIMIT:
            continue
        command = run_group.commands[args[1]] if args[0] == "run" else main.commands[args[0]]
        options = {opt for param in command.params for opt in param.opts}
        assert "--limit" not in options, f"{stage} could take a limit but isn't offered one"


def test_the_run_endpoint_passes_the_limit_through(monkeypatch, client):
    seen = {}

    from outreach.dashboard import Runner

    def fake_start(self, label, stages, limit=None, social_first=None):
        seen["stages"], seen["limit"] = stages, limit
        return True

    monkeypatch.setattr(Runner, "start", fake_start)
    client.post("/api/run", json={"what": "research", "limit": "7"})

    assert seen == {"stages": ["research"], "limit": 7}


def test_a_blank_how_many_means_the_whole_batch(monkeypatch, client):
    seen = {}

    from outreach.dashboard import Runner

    monkeypatch.setattr(
        Runner,
        "start",
        lambda self, label, stages, limit=None, social_first=None: (
            seen.update(limit=limit) or True
        ),
    )
    client.post("/api/run", json={"what": "research", "limit": ""})

    assert seen["limit"] is None


@pytest.mark.parametrize("bad", ["abc", "0", "-3", "999999"])
def test_a_nonsense_how_many_is_refused(client, bad):
    r = client.post("/api/run", json={"what": "research", "limit": bad})
    assert r.status_code == 400
    assert "How many" in r.get_json()["error"]


def test_the_page_offers_a_how_many_box(client):
    body = client.get("/").data
    assert b'id="howmany"' in body
    assert b"How many" in body


def test_the_target_card_exposes_the_batch_cap(client):
    assert b'id="t-batch"' in client.get("/").data


def test_the_batch_cap_can_be_raised_from_the_page(campaign_app):
    client, path = campaign_app
    d = client.post("/api/campaign", json={"batch_size": "25"}).get_json()

    assert d["batch_size"] == 25
    assert load_config(path, env_file=None).discovery_cap == 25


def test_a_nonsense_batch_cap_is_refused(campaign_app):
    client, path = campaign_app
    client.post("/api/campaign", json={"batch_size": "30"})  # force the fork
    before = path.read_text()

    assert client.post("/api/campaign", json={"batch_size": "lots"}).status_code == 400
    assert "at least 1" in client.post(
        "/api/campaign", json={"batch_size": "0"}
    ).get_json()["error"]
    assert path.read_text() == before


# --------------------------- the socials preference reaches every ranked stage


def test_the_socials_checkbox_reaches_the_brief_step():
    """It was only being passed to research, so briefing ignored it."""
    from outreach.dashboard import stage_args

    assert "--social-first" in stage_args("site-brief", social_first=True)
    assert "--sheet-order" in stage_args("site-brief", social_first=False)


def test_it_reaches_drafting_too():
    from outreach.dashboard import stage_args

    assert "--social-first" in stage_args("outreach-draft", social_first=True)


def test_discovery_is_never_given_it():
    """Discovery has no queue to rank -- it is finding the leads."""
    from outreach.dashboard import stage_args

    assert stage_args("discovery", social_first=True) == ["run", "discovery"]


def test_every_ranked_stage_accepts_the_flag_it_is_given():
    from outreach.cli import main
    from outreach.dashboard import RANKS_ITS_QUEUE, STAGE_COMMANDS

    run_group = main.commands["run"]
    for stage in RANKS_ITS_QUEUE:
        command = run_group.commands[STAGE_COMMANDS[stage][1]]
        options = {opt for param in command.params for opt in param.opts}
        assert "--social-first" in options, f"{stage} cannot take --social-first"


def test_a_full_run_carries_the_preference_into_every_step():
    from outreach.dashboard import FULL_RUN, RANKS_ITS_QUEUE, stage_args

    ranked = [s for s in FULL_RUN if s in RANKS_ITS_QUEUE]
    assert len(ranked) == 3, "research, site-brief and outreach-draft all rank"
    for stage in ranked:
        assert "--social-first" in stage_args(stage, social_first=True)


def test_the_review_queue_shows_the_social_leads_first():
    """You review one at a time, so the best should be the one in front of you."""
    leads = index(
        [
            {**lead_row("plain"), "Website Status": "none"},
            {**lead_row("fb"), "Website Status": "social_only"},
        ]
    )
    briefs = index([brief_row("plain"), brief_row("fb")])

    queue = review_queue(leads, {}, briefs)
    assert [q["lead_id"] for q in queue] == ["fb", "plain"]


# ------------------------------------------ seeing the queue before spending


def queued_lead(lead_id, stage, *, social=False, reviews=20):
    return {
        "Lead ID": lead_id,
        "Stage": stage,
        "Business Name": f"Biz {lead_id}",
        "Website Status": "social_only" if social else "none",
        "Review Count": str(reviews),
    }


def test_the_preview_shows_who_the_next_run_would_take():
    from outreach.dashboard import queue_preview

    leads = index(
        [
            queued_lead("old", "Researched"),
            queued_lead("new_fb", "Researched", social=True),
            queued_lead("fresh", "Discovered", social=True),
        ]
    )
    preview = queue_preview(leads, set(), set())

    assert [q["name"] for q in preview["site-brief"]] == ["Biz new_fb", "Biz old"]
    assert preview["site-brief"][0]["social"] is True
    assert [q["name"] for q in preview["research"]] == ["Biz fresh"]


def test_an_old_backlog_does_not_block_a_new_social_lead():
    """The question that prompted this: do I have to clear the Sheet first?"""
    from outreach.dashboard import queue_preview

    old = [queued_lead(f"old{i}", "Researched") for i in range(12)]
    new = [queued_lead(f"new{i}", "Researched", social=True) for i in range(5)]
    preview = queue_preview(index(old + new), set(), set(), limit=5)

    assert all(q["social"] for q in preview["site-brief"])


def test_the_preview_respects_sheet_order_when_ranking_is_off():
    from outreach.dashboard import queue_preview

    leads = index(
        [
            queued_lead("plain", "Researched"),
            queued_lead("fb", "Researched", social=True),
        ]
    )
    preview = queue_preview(leads, set(), set(), social_first=False)
    assert preview["site-brief"][0]["name"] == "Biz plain"


def test_already_done_leads_are_not_previewed():
    from outreach.dashboard import queue_preview

    leads = index([queued_lead("done", "Researched", social=True)])
    preview = queue_preview(leads, set(), {"done"})
    assert preview["site-brief"] == []


def test_the_page_has_somewhere_to_show_the_queue(client):
    assert b'id="nextup"' in client.get("/").data


def test_next_up_is_empty_without_a_sheet(client):
    assert client.get("/api/next-up").get_json() == {"queues": {}}


# --------------------------------------------- choosing and testing the model


@pytest.fixture
def model_app(tmp_path, monkeypatch):
    """An app on a throwaway campaign file and a throwaway .env."""
    from pathlib import Path

    src = Path("config/campaigns/penang-my.yaml")
    campaign = tmp_path / "campaign.yaml"
    campaign.write_text(src.read_text())
    (tmp_path / ".env").write_text("PLACES_API_KEY=abc\n")
    monkeypatch.chdir(tmp_path)

    from outreach.campaign_edit import local_path_for

    config = load_config(campaign, env_file=None)
    return (
        create_app(config).test_client(),
        local_path_for(campaign),
        tmp_path / ".env",
    )


def test_the_model_endpoint_reports_the_provider_and_key_state(model_app):
    client, _campaign, _env = model_app
    d = client.get("/api/model").get_json()

    assert d["provider"] == "anthropic"
    assert d["model"] == "claude-opus-5"
    assert d["key_env"] == "ANTHROPIC_API_KEY"
    assert d["guaranteed"] is True
    assert "ollama-cloud" in d["providers"]
    assert "gemini" in d["providers"], "Lyon can't pick a provider the card doesn't offer"


def test_the_api_key_is_never_sent_to_the_browser(model_app):
    """It only ever learns whether one is set."""
    client, _campaign, env_file = model_app
    env_file.write_text("OLLAMA_API_KEY=super-secret\n")
    client.post("/api/model", json={"provider": "ollama-cloud", "model": "kimi-k2.6"})

    body = client.get("/api/model").data.decode()

    assert "super-secret" not in body
    assert client.get("/api/model").get_json()["key_set"] is True


def test_switching_to_gemini_works_end_to_end_and_never_echoes_the_key(model_app):
    """Gemini rides the same wiring as every other OpenAI-compatible provider.

    Saving a Gemini key must behave exactly like saving an OpenRouter one --
    written to .env, reported only as present, and never once echoed back in
    any response body the browser can see.
    """
    from outreach.env_edit import is_set

    client, campaign, env_file = model_app
    d = client.post(
        "/api/model",
        json={"provider": "gemini", "model": "gemini-2.5-flash", "api_key": "gm-secret-value"},
    ).get_json()

    assert d["ok"] is True
    assert is_set(env_file, "GEMINI_API_KEY")
    assert "gm-secret-value" in env_file.read_text()

    cfg = load_config(campaign, env_file=None)
    assert cfg.research.provider == "gemini"
    assert cfg.research.model == "gemini-2.5-flash"

    info = client.get("/api/model").get_json()
    assert info["provider"] == "gemini"
    assert info["key_env"] == "GEMINI_API_KEY"
    assert info["key_set"] is True
    assert "gm-secret-value" not in client.get("/api/model").data.decode()
    # The whole rendered dashboard page too, not just the JSON endpoint.
    assert "gm-secret-value" not in client.get("/").data.decode()


def test_switching_provider_and_model_rewrites_the_campaign(model_app):
    client, campaign, _env = model_app
    d = client.post(
        "/api/model", json={"provider": "openrouter", "model": "moonshotai/kimi-k2-thinking"}
    ).get_json()

    assert d["ok"] is True
    cfg = load_config(campaign, env_file=None)
    assert cfg.research.provider == "openrouter"
    assert cfg.research.model == "moonshotai/kimi-k2-thinking"


def test_the_campaign_comments_survive_a_model_switch(model_app):
    client, campaign, _env = model_app
    before = campaign.with_name("campaign.yaml").read_text().count("#")

    client.post("/api/model", json={"provider": "ollama", "model": "llama3"})

    assert campaign.read_text().count("#") == before


def test_saving_a_key_writes_it_to_env_for_the_chosen_provider(model_app):
    from outreach.env_edit import is_set

    client, _campaign, env_file = model_app
    client.post(
        "/api/model",
        json={"provider": "ollama-cloud", "model": "kimi-k2.6", "api_key": "olm-123"},
    )

    assert is_set(env_file, "OLLAMA_API_KEY")
    assert "olm-123" in env_file.read_text()
    assert "PLACES_API_KEY=abc" in env_file.read_text(), "the rest of .env survives"


def test_a_blank_key_leaves_the_existing_one_alone(model_app):
    client, _campaign, env_file = model_app
    env_file.write_text("OLLAMA_API_KEY=keep-me\n")

    client.post("/api/model", json={"provider": "ollama-cloud", "model": "x", "api_key": ""})

    assert "keep-me" in env_file.read_text()


def test_local_ollama_takes_no_key(model_app):
    client, _campaign, _env = model_app
    r = client.post("/api/model", json={"provider": "ollama", "api_key": "pointless"})
    assert r.status_code == 400


def test_an_unknown_provider_is_refused(model_app):
    client, campaign, _env = model_app
    client.post("/api/model", json={"model": "claude-opus-5"})  # force the fork
    before = campaign.read_text()

    assert client.post("/api/model", json={"provider": "gpt5000"}).status_code == 400
    assert campaign.read_text() == before


def test_an_empty_model_name_is_refused_and_nothing_is_written(model_app):
    client, campaign, _env = model_app
    client.post("/api/model", json={"model": "claude-opus-5"})  # force the fork
    before = campaign.read_text()

    d = client.post("/api/model", json={"model": "   "}).get_json()

    assert "cannot be empty" in d["error"]
    assert campaign.read_text() == before


def test_a_rejected_switch_leaves_a_loadable_campaign(model_app):
    client, campaign, _env = model_app
    client.post("/api/model", json={"model": ""})
    client.post("/api/model", json={"provider": "nonsense"})

    assert load_config(live(campaign), env_file=None).research.model == "claude-opus-5"


def test_test_model_is_a_button_the_dashboard_can_press():
    from outreach.cli import main
    from outreach.dashboard import STAGE_COMMANDS

    assert STAGE_COMMANDS["test-model"] == ["test-model"]
    assert "test-model" in main.commands


def test_the_page_has_a_model_section(client):
    body = client.get("/").data
    assert b'id="model"' in body
    assert b'data-run="test-model"' in body


def test_the_first_save_forks_and_leaves_the_tracked_file_alone(campaign_app):
    """The bug this fixes: `git pull` refusing because the dashboard wrote here."""
    client, local = campaign_app
    template = local.with_name("campaign.yaml")
    before = template.read_text()

    client.post("/api/campaign", json={"category": "barber shop"})

    assert local.exists(), "settings should land in the local copy"
    assert "barber shop" in local.read_text()
    assert template.read_text() == before, "the tracked file is untouched"


def test_the_fork_is_recorded_in_env_so_every_command_follows_it(campaign_app):
    from outreach.env_edit import is_set

    client, local = campaign_app
    client.post("/api/campaign", json={"category": "cafe"})

    assert is_set(Path(".env"), "CAMPAIGN_CONFIG")
    assert str(local) in Path(".env").read_text()


def test_a_second_save_edits_the_same_local_file(campaign_app):
    client, local = campaign_app
    client.post("/api/campaign", json={"category": "cafe"})
    client.post("/api/campaign", json={"batch_size": "25"})

    text = local.read_text()
    assert 'category: "cafe"' in text
    assert "batch_size: 25" in text, "the second save must not reset the first"


def test_a_model_switch_forks_too(model_app):
    client, local, _env = model_app
    template = local.with_name("campaign.yaml")
    before = template.read_text()

    client.post("/api/model", json={"provider": "ollama", "model": "llama3"})

    assert local.exists()
    assert template.read_text() == before


# --------------------------------------------------- rewriting existing briefs


def test_rewrite_button_is_wired_to_the_real_cli_flag():
    from outreach.cli import main
    from outreach.dashboard import stage_args

    assert STAGE_COMMANDS["site-brief-rewrite"] == ["run", "site-brief", "--rewrite"]

    run_group = main.commands["run"]
    site_brief_cmd = run_group.commands["site-brief"]
    options = {opt for param in site_brief_cmd.params for opt in param.opts}
    assert "--rewrite" in options

    assert stage_args("site-brief-rewrite", 5) == [
        "run", "site-brief", "--rewrite", "--limit", "5",
    ]


def test_rewrite_takes_a_limit_like_its_non_rewrite_sibling():
    """The trap this guards: adding a new STAGE_COMMANDS entry whose args[1]
    happens to be an existing, --limit-taking stage (here, ``site-brief``)
    passes `test_no_stage_outside_the_set_is_given_a_limit` even if you
    forget to add the new key to TAKES_LIMIT -- because that test resolves
    the underlying `run` subcommand, not the dashboard key. So the omission
    has to be caught here, on the dashboard's own behaviour.
    """
    from outreach.dashboard import TAKES_LIMIT, stage_args

    assert "site-brief-rewrite" in TAKES_LIMIT
    assert "--limit" not in stage_args("site-brief-rewrite")
    assert "--limit" in stage_args("site-brief-rewrite", 3)


def test_the_page_offers_a_rewrite_button_with_its_cost_and_risk_named(client):
    """Lyon reads the page, not the handbook, before he presses a button that
    spends money and can silently invalidate a brief he already approved --
    so both have to be said right next to the button."""
    body = client.get("/").data.decode()

    assert 'data-run="site-brief-rewrite"' in body
    rewrite_pos = body.index('data-run="site-brief-rewrite"')
    nearby = body[rewrite_pos : rewrite_pos + 700]

    assert "model call" in nearby or "cost" in nearby.lower()
    assert "already read and approved" in nearby or "different" in nearby


def test_full_run_never_includes_the_rewrite():
    """Rewriting is a deliberate, occasional action -- never part of the
    everyday batch, or a template tweak would silently re-spend on every
    lead ever briefed."""
    assert "site-brief-rewrite" not in FULL_RUN


def test_the_rewrite_button_obeys_the_socials_first_tick():
    """A button that ignores the box beside it is the reported bug, again.

    Rewriting picks a queue exactly as writing does, and `_run_rewrite`
    already threads `social_first` through to `select_rewrite`. Only the
    dashboard wiring was missing, so the tick silently did nothing here.
    """
    from outreach.dashboard import RANKS_ITS_QUEUE, stage_args

    assert "site-brief-rewrite" in RANKS_ITS_QUEUE
    assert "--social-first" in stage_args("site-brief-rewrite", social_first=True)
    assert "--sheet-order" in stage_args("site-brief-rewrite", social_first=False)


def test_the_generate_button_is_wired_and_bounded():
    """A button with no command behind it looks like a broken feature."""
    from outreach.dashboard import STAGE_COMMANDS, TAKES_LIMIT, stage_args

    assert STAGE_COMMANDS["site-generate"] == ["run", "site-generate"]
    assert "site-generate" in TAKES_LIMIT
    assert stage_args("site-generate", 5) == ["run", "site-generate", "--limit", "5"]


def test_the_generate_button_says_what_it_costs_and_does_not_publish():
    """Lyon must not press this expecting it to put sites online."""
    from outreach.dashboard import PAGE

    assert 'data-run="site-generate"' in PAGE
    assert "Nothing is published" in PAGE


def test_every_dashboard_command_exists_in_the_cli():
    """Guards the whole table, not just the new row."""
    from outreach.cli import main
    from outreach.dashboard import STAGE_COMMANDS

    for key, args in STAGE_COMMANDS.items():
        group = main
        for token in args:
            if token.startswith("-"):
                continue
            group = group.commands[token] if hasattr(group, "commands") else group
        assert group is not None, f"{key} points at a command that does not exist"


def test_the_preview_shows_as_many_as_the_run_will_take():
    """Previewing six of a fifteen-lead run means nine are chosen with nobody
    looking, which is the opposite of what the preview is for."""
    from outreach.dashboard import queue_preview

    leads = {
        f"p{i}": (i + 2, {
            "Lead ID": f"p{i}", "Business Name": f"Salon {i}",
            "Stage": "Discovered", "Website Status": "none",
        })
        for i in range(20)
    }
    six = queue_preview(leads, set(), set(), limit=6)
    fifteen = queue_preview(leads, set(), set(), limit=15)

    assert len(six["research"]) == 6
    assert len(fifteen["research"]) == 15


def test_every_previewed_lead_carries_the_id_that_makes_it_actionable():
    """Without the ID the row is decoration -- there is nothing to drop."""
    from outreach.dashboard import queue_preview

    leads = {"a": (2, {
        "Lead ID": "a", "Business Name": "Sun Salon",
        "Stage": "Discovered", "Website Status": "social_only",
    })}
    preview = queue_preview(leads, set(), set())

    assert preview["research"][0]["lead_id"] == "a"
    assert preview["research"][0]["social"] is True


def test_the_preview_is_capped_so_sixty_is_not_a_wall():
    from outreach.dashboard import PREVIEW_CAP

    assert 6 <= PREVIEW_CAP <= 40, "a readable list, not every lead"


def test_dropping_a_lead_marks_it_disqualified_and_nothing_else(monkeypatch):
    """Disqualified is already skipped by every stage, so this needs no new
    machinery and cannot half-apply. It must touch no other column."""
    writes = []

    class FakeSheets:
        url = "https://sheet"

        def update_column(self, tab, column, values, *, user_edit=False):
            from outreach.sheets import guard_manual_column

            guard_manual_column(tab, column, user_edit=user_edit)
            writes.append((tab.title, column, dict(values)))
            return len(values)

    from outreach import dashboard as dash

    monkeypatch.setattr(dash, "_sheets_client", lambda config: FakeSheets())
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    client = create_app(config).test_client()

    r = client.post("/api/drop", json={"lead_id": "a"})

    assert r.get_json() == {"ok": True}
    assert writes == [("Leads", "Stage", {"a": "Disqualified"})]


def test_dropping_without_a_lead_id_is_refused():
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    client = create_app(config).test_client()

    assert client.post("/api/drop", json={}).status_code == 400


def test_the_next_up_endpoint_honours_the_how_many_box(monkeypatch):
    """Testing queue_preview directly does not cover the wiring.

    Dropping `limit=shown` from the endpoint leaves the function test green
    while the page silently goes back to showing six of fifteen.
    """
    from outreach import dashboard as dash
    from outreach import schema

    rows = {
        f"p{i}": (i + 2, {
            "Lead ID": f"p{i}", "Business Name": f"Salon {i}",
            "Stage": "Discovered", "Website Status": "none",
        })
        for i in range(20)
    }

    class FakeSheets:
        url = "https://sheet"

        def read_rows(self, tab):
            return rows if tab is schema.LEADS else {}

    monkeypatch.setattr(dash, "_sheets_client", lambda config: FakeSheets())
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    # The endpoint short-circuits without a Sheet, which would make every
    # assertion below vacuously pass on an empty payload.
    config = replace(config, secrets=replace(config.secrets, sheet_id="s"))
    client = create_app(config).test_client()

    body = client.get("/api/next-up?limit=15").get_json()
    assert len(body["queues"]["research"]) == 15
    assert body["shown"] == 15

    # And the cap holds, so 60 is not a wall of names.
    capped = client.get("/api/next-up?limit=60").get_json()
    assert capped["shown"] == dash.PREVIEW_CAP

    # A junk value must not 500 the page.
    assert client.get("/api/next-up?limit=abc").status_code == 200


def test_the_page_javascript_actually_parses():
    """The whole dashboard is one <script> block. One syntax error anywhere
    in it and *nothing* runs -- every panel sits on "Loading...", no request
    is ever made, and the server log looks perfectly healthy because it only
    ever served `GET /`. There is no visible error unless you open the
    browser console, which Lyon has no reason to do.

    This shipped once. PAGE is a Python string, so a JS escape needs doubling:
    `\\n` in the source becomes a real newline and breaks the string literal
    it sits in. Every other escape in this file is written `\\\\n` for exactly
    that reason.

    The page is rendered through Jinja before parsing. PAGE now carries
    `|tojson` consts inside <script>, and the raw `{{ }}` they sit in is not
    valid JS -- so the *rendered* page is what the browser actually runs, and
    that is what this check must parse. Rendering first is strictly the
    stronger guard: it also fails on a Jinja error, not only a JS one.

    Skipped where node is absent rather than approximated in Python -- a
    half-check that passes on a broken page is worse than no check.
    """
    import re
    import shutil
    import subprocess
    from flask import Flask, render_template_string

    from outreach.config import load_config
    from outreach.dashboard import PAGE

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available to parse the page script")

    # Render with the real default config so the tojson consts substitute into
    # real JSON string literals, exactly as a request would.
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    app = Flask(__name__)
    with app.app_context():
        page = render_template_string(
            PAGE,
            campaign=config.campaign,
            build=config.build,
            whatsapp_enabled=config.outreach.whatsapp_enabled,
        )

    blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert blocks, "the page has no script block -- did the markup change?"

    for i, js in enumerate(blocks):
        proc = subprocess.run(
            [node, "--check", "-"], input=js, capture_output=True, text=True
        )
        assert proc.returncode == 0, (
            f"script block {i} does not parse:\n{proc.stderr}"
        )
