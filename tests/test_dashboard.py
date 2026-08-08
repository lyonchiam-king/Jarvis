"""The dashboard's numbers, and that it stays local and read-only."""

import ast
from pathlib import Path

import pytest

from outreach.config import load_config
from outreach.dashboard import FULL_RUN, STAGE_COMMANDS, create_app, summarise

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
    assert b"Ready to build" in body
    assert b"Copy prompt" in body
    assert b"lovable.dev" in body
    assert b"Mark as built" in body


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
