"""The dashboard's numbers, and that it stays local and read-only."""

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


def test_page_offers_the_copy_and_open_lovable_flow(client):
    body = client.get("/").data
    assert b"Copy prompt" in body
    assert b"lovable.dev" in body
    assert b"Mark as built" in body


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
def campaign_app(tmp_path):
    """An app pointed at a throwaway copy of the real campaign file."""
    from pathlib import Path

    src = Path("config/campaigns/penang-my.yaml")
    path = tmp_path / "campaign.yaml"
    path.write_text(src.read_text())
    config = load_config(path, env_file=None)
    return create_app(config).test_client(), path


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
    before = path.read_text().count("#")
    client.post("/api/campaign", json={"category": "cafe", "areas": ["Ipoh"]})

    assert path.read_text().count("#") == before


def test_an_empty_category_is_refused_and_nothing_is_written(campaign_app):
    client, path = campaign_app
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

    cfg = load_config(path, env_file=None)
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
    before = path.read_text()

    assert client.post("/api/campaign", json={"batch_size": "lots"}).status_code == 400
    assert "at least 1" in client.post(
        "/api/campaign", json={"batch_size": "0"}
    ).get_json()["error"]
    assert path.read_text() == before
