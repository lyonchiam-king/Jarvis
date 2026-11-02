"""The dashboard's numbers, and that it stays local and read-only."""

from dataclasses import replace
import ast
import re
import threading
import time
from pathlib import Path

import pytest

from outreach import schema
from outreach import dashboard as dash
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


def test_already_sent_whatsapp_is_not_counted_as_waiting():
    # The send queue hides any drafted WhatsApp lead whose WhatsApp Status is
    # sent/delivered/read -- it treats those as already gone. The top count
    # must agree, or "WhatsApp to send" reads 1 while section 4 is empty: the
    # bug that had a founder think their drafted message never landed.
    for already in ("sent", "delivered", "read"):
        outreach = {
            "b": (3, {
                "Channel": "WhatsApp",
                "Status": "Drafted",
                "WhatsApp Status": already,
            }),
        }
        s = summarise({}, set(), set(), outreach, {})
        assert s.whatsapp_waiting == 0, (
            f"WhatsApp Status={already!r} is already sent -- must not count as waiting"
        )


def test_failed_whatsapp_still_counts_as_waiting():
    # "failed" is not sent -- the queue still shows it for Retry, so the count
    # must still include it. Guards the fix from over-excluding.
    outreach = {
        "b": (3, {
            "Channel": "WhatsApp",
            "Status": "Drafted",
            "WhatsApp Status": "failed",
        }),
    }
    s = summarise({}, set(), set(), outreach, {})
    assert s.whatsapp_waiting == 1


def test_whatsapp_row_with_empty_status_but_draft_markers_still_counts():
    # A row whose Status cell was cleared by hand still carries a drafted
    # message and timestamp, so the "WhatsApp to send" count must agree with
    # section 4 and not drop it.
    outreach = {
        "b": (3, {
            "Channel": "WhatsApp",
            "Status": "",
            "WhatsApp Message": "Hi, we built you a site!",
            "Drafted At": "2026-08-09 10:00:00",
        }),
    }
    s = summarise({}, set(), set(), outreach, {})
    assert s.whatsapp_waiting == 1


def test_unreachable_rows_are_not_counted_as_waiting():
    outreach = {
        "a": (2, {"Channel": "Unreachable", "Status": "Drafted"}),
        "b": (3, {"Channel": "WhatsApp", "Status": "Drafted"}),
    }
    s = summarise({}, set(), set(), outreach, {})

    assert s.emails_waiting == 0
    assert s.whatsapp_waiting == 1, "unreachable leads have nothing to send"


def test_unreachable_leads_show_in_the_queue_as_excluded(monkeypatch):
    """A landline-only lead is drafted as Channel=Unreachable. It can't be
    sent, but it must still show at the bottom of section 4 with the reason --
    hiding it is the bug that made a founder think the draft never saved."""
    from outreach import dashboard as dash
    from outreach import schema

    outreach = {
        "land": (2, {
            "Lead ID": "land",
            "Business Name": "Kedai Kopi",
            "Channel": "Unreachable",
            "Status": "Drafted",
            "WhatsApp Message": "Hi, we built you a site!",
            "Drafted At": "2026-08-16 10:00:00",
        }),
    }
    leads = {
        "land": (2, {
            "Lead ID": "land",
            "Business Name": "Kedai Kopi",
            "Phone": "04-1234567",
        }),
    }

    class FakeSheets:
        url = "https://sheet"

        def read_rows(self, tab):
            if tab is schema.OUTREACH_LOG:
                return outreach
            if tab is schema.LEADS:
                return leads
            return {}

    monkeypatch.setattr(dash, "_sheets_client", lambda config: FakeSheets())
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, sheet_id="s"))
    client = create_app(config).test_client()

    body = client.get("/api/whatsapp/queue").get_json()

    assert body["queue"] == []
    assert [e["reason"] for e in body["excluded"]] == ["unreachable"]
    assert body["excluded"][0]["name"] == "Kedai Kopi"
    assert body["excluded"][0]["phone"] == "04-1234567"


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
    response = client.get("/classic")
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


def _serve_with(monkeypatch, config, **kwargs):
    """Call serve() with the server stubbed, and report the host it asked for.

    Exercises the real function rather than parsing the source for a literal:
    the guarantee is about what it does, and the host stopped being a
    constant the moment a phone had to reach it.
    """
    from outreach import dashboard as dash

    seen = {}

    class FakeApp:
        def run(self, host=None, port=None, debug=None):
            seen["host"] = host
            seen["port"] = port

    monkeypatch.setattr(dash, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(dash, "create_app", lambda cfg: FakeApp())
    dash.serve(open_browser=False, **kwargs)
    return seen


def test_the_dashboard_serves_only_this_machine_by_default(monkeypatch):
    """It must not be reachable from the network unless asked -- it can spend
    money and message real businesses."""
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)

    assert _serve_with(monkeypatch, config)["host"] == "127.0.0.1"


def test_serving_to_the_network_without_a_password_is_refused(monkeypatch):
    """The one hard rule. On a cafe or hotel network everyone in the building
    is on that network, and the dashboard has no other protection -- so this
    is refused rather than warned about."""
    from outreach.dashboard import UnsafeBinding

    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, dashboard_password=""))

    with pytest.raises(UnsafeBinding) as exc:
        _serve_with(monkeypatch, config, host="0.0.0.0")

    message = str(exc.value)
    assert "DASHBOARD_PASSWORD" in message, "the message must name the fix"
    assert ".env" in message


def test_serving_to_the_network_is_allowed_once_a_password_is_set(monkeypatch):
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(
        config, secrets=replace(config.secrets, dashboard_password="hunter2")
    )

    assert _serve_with(monkeypatch, config, host="0.0.0.0")["host"] == "0.0.0.0"


def test_the_wide_address_is_named_once_and_guarded_where_it_is_used():
    """0.0.0.0 must not be reachable by accident. It is one named constant,
    used in one expression, and that expression requires a password."""
    source = (SRC / "dashboard.py").read_text()

    assert source.count('"0.0.0.0"') == 1, "the wide address is defined once"
    assert "ALL_INTERFACES = " in source
    # The CLI passes intent, never an address, so it cannot widen it alone.
    assert "0.0.0.0" not in (SRC / "cli.py").read_text()

    # The expression that chooses it is not asserted here. This test used to
    # hold one literal line, which meant a second way of reaching 0.0.0.0
    # could be added without it noticing, and a harmless rewrite of the same
    # rule failed it. `test_no_route_to_the_wide_address_skips_the_password`
    # drives `serve` and asserts the property instead.


def test_no_route_to_the_wide_address_skips_the_password(monkeypatch, tmp_path):
    """Every way of asking to be reachable, with no password set.

    There are two now -- `--phone` and a hosted deployment -- and the point
    is that neither is a separate decision about safety. The dashboard spends
    money, messages real businesses and reads the Sheet; on any network wider
    than this machine the password is the only thing in front of it.
    """
    from outreach import dashboard as dash
    from outreach import paths

    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, dashboard_password=""))

    for hosted in (False, True):
        if hosted:
            monkeypatch.setenv(paths.ENV_VAR, str(tmp_path))
        else:
            monkeypatch.delenv(paths.ENV_VAR, raising=False)

        for phone in (False, True):
            bound = []
            monkeypatch.setattr(dash, "load_config", lambda *a, **k: config)
            monkeypatch.setattr(
                dash, "create_app",
                lambda c: type("App", (), {
                    "config": {"state": {}},
                    "run": lambda self, **kw: bound.append(kw.get("host")),
                    "update": lambda self, **kw: None,
                })(),
            )
            try:
                dash.serve(open_browser=False, phone=phone)
            except dash.UnsafeBinding:
                pass  # Refused outright, which is the strongest answer.
            assert dash.ALL_INTERFACES not in bound, (
                f"reachable with no password (hosted={hosted}, phone={phone})"
            )


def test_asking_for_phone_access_without_a_password_stays_local(monkeypatch):
    """The failure that made this "not working": the launcher decided, got it
    wrong, and said nothing. Asking now always produces an answer -- either
    the network binding or a local one that explains itself."""
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, dashboard_password=""))

    seen = _serve_with(monkeypatch, config, phone=True)

    assert seen["host"] == "127.0.0.1", "no password means no network binding"


def test_asking_for_phone_access_with_a_password_serves_the_network(monkeypatch):
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(
        config, secrets=replace(config.secrets, dashboard_password="hunter2")
    )

    assert _serve_with(monkeypatch, config, phone=True)["host"] == "0.0.0.0"


def test_the_reason_phone_access_is_off_is_printed(monkeypatch, capsys):
    """Silence is what made the last attempt undebuggable."""
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, dashboard_password=""))

    _serve_with(monkeypatch, config, phone=True)

    out = capsys.readouterr().out
    assert "Phone access is OFF" in out
    assert "DASHBOARD_PASSWORD" in out, "the message must name the fix"


def test_the_address_to_type_on_the_phone_is_printed(monkeypatch, capsys):
    """So nobody has to run ipconfig and pick the right line out of it."""
    from outreach import dashboard as dash

    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(
        config, secrets=replace(config.secrets, dashboard_password="hunter2")
    )
    monkeypatch.setattr(dash, "lan_addresses", lambda: ["192.168.1.42"])

    _serve_with(monkeypatch, config, phone=True)

    out = capsys.readouterr().out
    assert "http://192.168.1.42:8765" in out
    # The firewall prompt is the most common reason a correct setup fails.
    assert "firewall" in out.lower()


def test_a_machine_that_cannot_name_its_address_says_so(monkeypatch, capsys):
    from outreach import dashboard as dash

    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(
        config, secrets=replace(config.secrets, dashboard_password="hunter2")
    )
    monkeypatch.setattr(dash, "lan_addresses", lambda: [])

    _serve_with(monkeypatch, config, phone=True)

    out = capsys.readouterr().out
    assert "ipconfig" in out


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


def _client_with_leads(rows, monkeypatch):
    """A test client whose Sheet holds exactly these Leads rows, so a
    record-build can be observed end to end without a real Sheet."""
    writes = []

    class FakeSheets:
        url = "https://sheet"

        def read_rows(self, tab):
            return {rid: (i + 2, row) for i, (rid, row) in enumerate(rows)} \
                if tab is schema.LEADS else {}

        def update_column(self, tab, column, values, **k):
            writes.append((tab, column, dict(values)))
            return len(values)

    monkeypatch.setattr(dash, "_sheets_client", lambda config: FakeSheets())
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, sheet_id="s"))
    return create_app(config).test_client(), writes


def test_record_build_refuses_a_disqualified_lead(monkeypatch):
    """Recording the build anyway would drop them straight back into the
    outreach queue with a live site behind the message -- the one outcome
    "do not contact this business" exists to prevent."""
    c, writes = _client_with_leads([("a", {"Lead ID": "a", "Stage": "Disqualified"})],
                                   monkeypatch)

    body = c.post(
        "/api/record-build",
        json={"lead_id": "a", "preview_url": "https://p/a"},
    ).get_json()

    assert "disqualified" in body["error"]
    assert writes == [], "nothing written for a lead who asked to be left alone"


def test_record_build_never_moves_a_lead_backwards(monkeypatch):
    """The button lands after the message has gone out by hand sometimes; a
    lead already at Email Drafted must not be dragged back to Site Built."""
    c, writes = _client_with_leads([("a", {"Lead ID": "a", "Stage": "Email Drafted"})],
                                   monkeypatch)

    body = c.post("/api/record-build", json={"lead_id": "a"}).get_json()
    assert body == {"ok": True}
    stage_writes = [w for w in writes if w[1] == "Stage"]
    assert stage_writes == [(schema.LEADS, "Stage", {"a": "Email Drafted"})]


def test_build_queue_is_empty_without_a_sheet(client):
    assert client.get("/api/build-queue").get_json() == {"queue": []}


def test_page_offers_the_copy_and_open_builder_flow(client):
    body = client.get("/classic").data
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
    body = client.get("/classic").data.decode()
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
    body = client.get("/classic").data.decode()
    assert "paste into Google AI Studio" in body
    assert 'const SITE_BUILDER = "Google AI Studio"' in body
    assert 'const SITE_BUILDER_URL = "https://aistudio.google.com/"' in body


def test_build_panel_open_button_when_url_blank():
    """A blank URL must not render a dead link. We chose to omit the anchor and
    render a bare button -- assert that choice so the edge stays handled."""
    client = _client_with_build(site_builder_name="Acme Builder", site_builder_url="")
    body = client.get("/classic").data.decode()
    assert 'const SITE_BUILDER_URL = ""' in body
    # The queue-card template literal renders a bare button when the URL is
    # blank -- no anchor wrapping it, and never a placeholder href.
    assert "`<button>Open ${SITE_BUILDER}</button>`" in body
    assert "href=\"#\"" not in body


def test_page_lays_the_morning_out_in_order(client):
    """Gather, review, build, send -- the order you actually work in."""
    body = client.get("/classic").data.decode()
    headings = [f"</span> {s}" for s in ("Gather", "Review", "Build", "Send")]
    for heading in headings:
        assert heading in body, f"missing step heading: {heading}"
    positions = [body.index(h) for h in headings]
    assert positions == sorted(positions)


def test_the_review_step_offers_approve_and_skip(client):
    body = client.get("/classic").data
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
    body = client.get("/classic").data
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
    assert b"cafe" in client.get("/classic").data


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
    assert b'href="/guide"' in client.get("/classic").data


def test_each_step_links_to_its_place_in_the_handbook(client):
    """A help link that 404s inside the page is worse than none."""
    from outreach.guide import guide_html

    page = client.get("/classic").data.decode()
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
    from outreach import env_edit

    monkeypatch.setattr(dash, "_load_summary", lambda config: dash.Summary(total=7))
    # The endpoint also checks the campaign's model key with is_set, which
    # reads the real .env on this machine -- patch it so the test does not
    # depend on what happens to be in that file.
    monkeypatch.setattr(env_edit, "is_set", lambda path, name: True)
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
    body = client.get("/classic").data
    assert b'id="howmany"' in body
    assert b"How many" in body


def test_the_target_card_exposes_the_batch_cap(client):
    assert b'id="t-batch"' in client.get("/classic").data


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
    # old and new_fb have Research rows (the normal case); nobody has been
    # briefed yet. A past-stage lead with NO output row is healed into the
    # queue rather than skipped -- that contract lives in test_selection.py.
    preview = queue_preview(leads, {"old", "new_fb"}, set())

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
    assert b'id="nextup"' in client.get("/classic").data


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
    assert "gm-secret-value" not in client.get("/classic").data.decode()


def test_switching_provider_and_model_rewrites_the_campaign(model_app):
    client, campaign, _env = model_app
    d = client.post(
        "/api/model", json={"provider": "openrouter", "model": "moonshotai/kimi-k2-thinking"}
    ).get_json()

    assert d["ok"] is True
    cfg = load_config(campaign, env_file=None)
    assert cfg.research.provider == "openrouter"
    assert cfg.research.model == "moonshotai/kimi-k2-thinking"


def test_the_model_endpoint_reports_the_outreach_model(model_app):
    client, _campaign, _env = model_app
    d = client.get("/api/model").get_json()
    assert d["outreach_model"] == "", "blank means 'use the research model'"


def test_setting_the_outreach_model_rewrites_the_campaign(model_app):
    client, campaign, _env = model_app
    d = client.post(
        "/api/model",
        json={"model": "claude-opus-5", "outreach_model": "qwen3:cloud"},
    ).get_json()

    assert d["ok"] is True
    cfg = load_config(campaign, env_file=None)
    assert cfg.outreach.model == "qwen3:cloud"
    assert cfg.research.model == "claude-opus-5", "the research model is untouched"


def test_clearing_the_outreach_model_goes_back_to_inherit(model_app):
    client, campaign, _env = model_app
    client.post("/api/model", json={"outreach_model": "qwen3:cloud"})
    assert load_config(campaign, env_file=None).outreach.model == "qwen3:cloud"

    client.post("/api/model", json={"outreach_model": ""})
    assert load_config(campaign, env_file=None).outreach.model == ""


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
    body = client.get("/classic").data
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
    body = client.get("/classic").data.decode()

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


def test_the_page_never_calls_an_undefined_function():
    """node --check above catches syntax, not this. A misspelled function name
    parses perfectly and dies only when its panel calls it -- which on this
    page means that section sits on "Loading..." with no error the server log
    would show. This walks the rendered script and holds every bare call
    against the names actually defined on the page.

    Deliberately approximate: strings (and the ${...} inside them) are stripped
    first so a name in a template literal cannot lie, and anything defined is
    collected generously. The risk this runs is a false alarm on a name it
    could not see defined -- a fixable false positive, never a silent pass.
    """
    import re
    from flask import Flask, render_template_string

    from outreach.config import load_config
    from outreach.dashboard import PAGE

    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    app = Flask(__name__)
    with app.app_context():
        page = render_template_string(
            PAGE,
            campaign=config.campaign,
            build=config.build,
            whatsapp_enabled=config.outreach.whatsapp_enabled,
        )

    builtins = {
        "$", "document", "window", "location", "navigator", "history",
        "fetch", "alert", "confirm", "encodeURIComponent", "decodeURIComponent",
        "setTimeout", "setInterval", "clearTimeout", "clearInterval",
        "requestAnimationFrame", "parseInt", "parseFloat", "isNaN", "Number",
        "String", "Boolean", "Object", "Array", "JSON", "Math", "Date", "Set",
        "Map", "URL", "URLSearchParams", "URLPattern", "AbortController",
        "FormData", "Blob", "FileReader", "Error", "TypeError", "Promise",
        "localStorage", "sessionStorage", "structuredClone", "crypto",
        "escape", "unescape", "Event", "CustomEvent", "HTMLElement",
        "getComputedStyle", "matchMedia", "queueMicrotask",
    }
    keywords = {
        "if", "for", "while", "switch", "catch", "return", "await", "typeof",
        "function", "do", "else", "yield", "new", "delete", "void", "case",
        "async",
    }

    defined = set()
    calls = set()
    for js in re.findall(r"<script>(.*?)</script>", page, re.S):
        # Strip strings, comments and declarations-as-calls so only real call
        # sites remain. Anything lost here only shrinks the check, never
        # breaks it -- but note that stripping `${...}` also drops code inside
        # template literals, so a helper *defined* only inside a backtick
        # would be under-counted. The page defines none that way.
        js = re.sub(r"`(?:\\.|[^`])*`", " ", js)
        js = re.sub(r'"(?:\\.|[^"\\\n])*"', " ", js)
        js = re.sub(r"'(?:\\.|[^'\\\n])*'", " ", js)
        js = re.sub(r"//[^\n]*", " ", js)
        js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)

        defined |= set(re.findall(r"\bclass\s+([A-Za-z_$][\w$]*)\b", js))
        defined |= set(re.findall(r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\b", js))
        defined |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b", js))
        # Lowercase identifiers followed by (...) and a brace are almost
        # always a declaration site (method shorthand, if/for/catch); count
        # them as defined rather than chased -- over-approximating definitions
        # is safe, under-approximating calls is not.
        defined |= {
            m.group(1)
            for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*\([^()]{0,200}\)\s*\{", js)
        }
        calls |= {
            m.group(1)
            for m in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", js)
        }

    unknown = sorted(calls - defined - builtins - keywords)
    assert not unknown, (
        "the page calls names nothing defines; in one script block that is "
        f"a dead panel: {unknown}"
    )


def test_every_stage_button_is_gated_until_setup_finishes():
    """A new user's first click on "Run everything" must be stopped while
    there is no Sheet to write to, so the page names the stage buttons in
    SETUP_LOCKED and disables them until first-run setup is done. The list is
    hand-written, though -- a button renamed on the page without updating the
    list silently escapes the gate. This holds both directions: every locked
    name is a real button, and the read-only buttons stay clickable."""
    import re
    from flask import Flask, render_template_string

    from outreach.config import load_config
    from outreach.dashboard import PAGE

    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    app = Flask(__name__)
    with app.app_context():
        page = render_template_string(
            PAGE,
            campaign=config.campaign,
            build=config.build,
            whatsapp_enabled=config.outreach.whatsapp_enabled,
        )

    match = re.search(r"SETUP_LOCKED = new Set\(\[([^\]]+)\]\)", page, re.S)
    assert match, "SETUP_LOCKED is gone from the page"
    locked = set(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)))

    buttons = set(re.findall(r'data-run=["\']([^"\']+)["\']', page))
    assert locked <= buttons, (
        f"locked names that match no button, so the gate is dead weight: "
        f"{sorted(locked - buttons)}"
    )

    # Reading costs nothing and needs no setup: these must never be gated.
    unlocked = {"preview-leads", "preview-messages", "preflight", "test-model"}
    assert unlocked <= buttons
    assert not (locked & unlocked), (
        f"read-only buttons gated behind setup: {sorted(locked & unlocked)}"
    )


class TestSharedSheetsCache:
    """The dashboard reads the same five tabs from four panels on one tick.

    Sheets allows 60 reads a minute per user. Before this cache an idle page
    spent 26 of them, and a drafting run -- whose subprocess reads the Sheet
    on the same quota -- pushed the total past the limit, blanking section 4
    with a 429. These tests pin both halves: that reads are actually shared,
    and that sharing never outlives a write.
    """

    class Counting:
        """Stands in for SheetsClient, counting reads per tab."""

        def __init__(self, fail=None):
            self.reads = {}
            self.fail = fail or {}
            self.url = "https://sheet"

        def read_rows(self, tab):
            self.reads[tab.title] = self.reads.get(tab.title, 0) + 1
            if tab.title in self.fail:
                raise self.fail[tab.title]
            return {"lead-1": (2, {"Lead ID": "lead-1", "Business Name": tab.title})}

        def upsert(self, tab, rows, **kw):
            return None

        def update_column(self, tab, column, values, **kw):
            return len(values)

        def append(self, tab, rows):
            return len(rows)

    def test_repeated_reads_of_one_tab_hit_the_sheet_once(self):
        inner = self.Counting()
        shared = dash._SharedSheets(inner)

        for _ in range(9):
            shared.read_rows(schema.LEADS)

        assert inner.reads["Leads"] == 1

    def test_each_tab_is_cached_separately(self):
        inner = self.Counting()
        shared = dash._SharedSheets(inner)

        for tab in (schema.LEADS, schema.RESEARCH, schema.LEADS, schema.RESEARCH):
            shared.read_rows(tab)

        assert inner.reads == {"Leads": 1, "Research": 1}

    def test_an_expired_entry_is_read_again(self):
        inner = self.Counting()
        shared = dash._SharedSheets(inner, ttl=0)

        shared.read_rows(schema.LEADS)
        shared.read_rows(schema.LEADS)

        assert inner.reads["Leads"] == 2

    def test_a_write_invalidates_so_the_next_read_sees_it(self):
        """The failure this guards: approve a brief, the panel re-reads, and
        the cache hands back the snapshot from before the approval -- so the
        button looks like it did nothing."""
        inner = self.Counting()
        shared = dash._SharedSheets(inner)

        shared.read_rows(schema.SITE_BRIEFS)
        shared.update_column(
            schema.SITE_BRIEFS, "Approval Status", {"lead-1": "Approved"},
            user_edit=True,
        )
        shared.read_rows(schema.SITE_BRIEFS)

        assert inner.reads["Site Briefs"] == 2

    def test_a_write_that_raises_still_invalidates(self):
        class Boom(self.Counting):
            def update_column(self, *a, **k):
                raise RuntimeError("sheet write failed")

        inner = Boom()
        shared = dash._SharedSheets(inner)
        shared.read_rows(schema.LEADS)

        with pytest.raises(RuntimeError):
            shared.update_column(schema.LEADS, "Stage", {"lead-1": "x"})

        # A failed write may still have landed; assuming it didn't is how a
        # stale panel outlives the thing that changed.
        shared.read_rows(schema.LEADS)
        assert inner.reads["Leads"] == 2

    def test_a_read_failure_serves_the_previous_rows_not_a_blank_page(self):
        """'Read requests per minute' clears by waiting. Dropping a whole
        panel over a limit that heals in seconds is worse than being a few
        seconds stale."""
        inner = self.Counting()
        shared = dash._SharedSheets(inner, ttl=0)
        first = shared.read_rows(schema.OUTREACH_LOG)

        inner.fail["Outreach Log"] = RuntimeError("HTTP 429: quota exceeded")
        assert shared.read_rows(schema.OUTREACH_LOG) == first

    def test_a_read_that_never_succeeded_still_raises(self):
        inner = self.Counting(fail={"Errors": RuntimeError("HTTP 429")})
        shared = dash._SharedSheets(inner)

        with pytest.raises(RuntimeError):
            shared.read_rows(schema.ERRORS)

    def test_unknown_attributes_fall_through_to_the_real_client(self):
        shared = dash._SharedSheets(self.Counting())
        assert shared.url == "https://sheet"

    def test_a_finished_run_invalidates_the_cache(self, monkeypatch):
        """A stage writes from a subprocess, so this process never saw it.
        Without this the page re-reads a snapshot taken before the run and the
        freshly drafted messages are missing -- the original bug, again."""
        cleared = []
        monkeypatch.setattr(dash, "_invalidate_sheets_cache",
                            lambda: cleared.append(True))
        monkeypatch.setattr(dash.subprocess, "Popen", _FakePopen)

        runner = dash.Runner()
        runner.start("draft", ["outreach-draft"])
        for _ in range(200):
            if runner.running is None:
                break
            time.sleep(0.01)

        assert runner.running is None, "run did not finish"
        assert cleared, "a finished run must drop the cached reads"

    def test_a_run_starts_with_no_readable_stdin(self, monkeypatch):
        """A subprocess that inherits this console can be asked a question
        the page never shows: the build's y/n prompt hung a whole run for
        ever. With stdin at the null device the CLI's confirm helper sees no
        terminal and skips instead of waiting for an answer nobody can give."""
        captured = {}

        def fake_popen(*args, **kwargs):
            captured.update(kwargs)
            return _FakePopen(*args, **kwargs)

        monkeypatch.setattr(dash, "_invalidate_sheets_cache", lambda: None)
        monkeypatch.setattr(dash.subprocess, "Popen", fake_popen)

        runner = dash.Runner()
        runner.start("draft", ["outreach-draft"])
        for _ in range(200):
            if runner.running is None:
                break
            time.sleep(0.01)

        assert runner.running is None, "run did not finish"
        assert captured["stdin"] is dash.subprocess.DEVNULL


class _FakePopen:
    """A subprocess that prints one line and exits cleanly."""

    def __init__(self, *a, **k):
        self.stdout = iter(["done\n"])
        self.returncode = 0

    def wait(self):
        return 0


class TestPerThreadTransport:
    """httplib2 is not thread-safe, and the dev server is threaded.

    Sharing one SheetsClient across request threads put several of them on one
    TLS socket; the frames interleaved and Sheets reads came back as
    ``SSLError: WRONG_VERSION_NUMBER``. The cache in front may be shared -- it
    holds plain dicts -- but the transport underneath may not.
    """

    def test_each_thread_gets_its_own_client(self, monkeypatch):
        built = []

        class FakeClient:
            def __init__(self, creds, sheet_id):
                built.append(threading.current_thread().name)

        monkeypatch.setattr("outreach.auth.get_credentials", lambda *a, **k: object())
        monkeypatch.setattr("outreach.sheets.SheetsClient", FakeClient)

        per_thread = dash._PerThreadClient(("client.json", "token.json", "sheet-1"))
        # Hold the objects, not their id()s: a finished thread's client is
        # freed and CPython hands the same address to the next one, which
        # makes an id-based check pass or fail for reasons of its own.
        seen = []

        def use():
            seen.append(per_thread._client())

        threads = [threading.Thread(target=use, name=f"w{i}") for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(built) == 4, "each thread must build its own transport"
        assert len({id(c) for c in seen}) == 4, "threads must not share a client"

    def test_the_same_thread_reuses_its_client(self, monkeypatch):
        built = []

        class FakeClient:
            def __init__(self, creds, sheet_id):
                built.append(1)

        monkeypatch.setattr("outreach.auth.get_credentials", lambda *a, **k: object())
        monkeypatch.setattr("outreach.sheets.SheetsClient", FakeClient)

        per_thread = dash._PerThreadClient(("client.json", "token.json", "sheet-1"))
        first = per_thread._client()
        assert per_thread._client() is first
        assert len(built) == 1

    def test_concurrent_reads_through_the_cache_stay_consistent(self, monkeypatch):
        """The shared cache must survive parallel use without tearing."""
        class Counting:
            def __init__(self):
                self.n = 0
                self.lock = threading.Lock()

            def read_rows(self, tab):
                with self.lock:
                    self.n += 1
                time.sleep(0.005)  # widen the window for a race
                return {"lead-1": (2, {"Lead ID": "lead-1"})}

        inner = Counting()
        shared = dash._SharedSheets(inner)
        results = []

        def read():
            results.append(shared.read_rows(schema.LEADS))

        threads = [threading.Thread(target=read) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 8
        assert all(r == results[0] for r in results)
        # Single-flight: eight simultaneous callers, one request. Without it
        # every panel misses at the same instant on a cold cache and the cache
        # saves nothing precisely when the quota is tightest.
        assert inner.n == 1


def test_the_leads_to_research_count_excludes_refused_leads(monkeypatch):
    """The count has to agree with what Research will actually do.

    Research skips a lead WhatsApp has refused, so counting it as work
    outstanding promises a batch that will not happen and makes the run look
    like it under-delivered -- the same mismatch Lyon hit when the check said
    six were reachable and Research went through all of them.
    """
    from outreach import dashboard as dash

    leads = {
        "yes": (2, {"Lead ID": "yes", "Stage": "Discovered",
                    "WhatsApp Reachable": "yes"}),
        "no": (3, {"Lead ID": "no", "Stage": "Discovered",
                   "WhatsApp Reachable": "no"}),
        "unchecked": (4, {"Lead ID": "unchecked", "Stage": "Discovered"}),
    }

    summary = dash.summarise(leads, set(), set(), {}, {}, require_site=False)

    # The reachable one and the unchecked one; never the refused one.
    assert summary.to_research == 2


# ------------------------------------------- first run: their own Sheet


def _first_run_config():
    """A config as a brand-new user has it: signed in to nothing, no Sheet.

    Category and location are pinned, not inherited from the live campaign
    file, because the Sheet's title is built from them.
    """
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    return replace(
        config,
        campaign=replace(config.campaign, category="hair salon", location="Penang"),
        secrets=replace(config.secrets, sheet_id=""),
    )


class _FakeNewSheets:
    """Stands in for SheetsClient on a Sheet that does not exist yet."""

    made: list = []

    def __init__(self, credentials, spreadsheet_id=""):
        self.credentials = credentials
        self.spreadsheet_id = spreadsheet_id
        self.tabs = None

    def create_spreadsheet(self, title):
        self.spreadsheet_id = "new-sheet-id"
        _FakeNewSheets.made.append(title)
        return self.spreadsheet_id

    def ensure_tabs(self, tabs):
        self.tabs = tuple(tabs)
        return []

    @property
    def url(self):
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit"


@pytest.fixture
def new_user(monkeypatch, tmp_path):
    """The dashboard as a new user meets it, with Google and .env faked out.

    The .env write is redirected into tmp_path deliberately: a test that
    wrote SHEET_ID into the real file would break every command on the
    machine running the suite, which is the bug conftest.py exists to catch.
    """
    from outreach import auth, env_edit, sheets

    _FakeNewSheets.made = []
    written = {}
    env = tmp_path / ".env"

    monkeypatch.setattr(
        auth, "get_credentials", lambda *a, **kw: "creds-for-this-person"
    )
    monkeypatch.setattr(sheets, "SheetsClient", _FakeNewSheets)
    monkeypatch.setattr(
        env_edit, "set_key",
        lambda path, name, value: written.__setitem__(name, (path, value)),
    )
    monkeypatch.delenv("SHEET_ID", raising=False)
    monkeypatch.setattr(dash, "load_config", lambda *a, **kw: _first_run_config())
    yield {"written": written, "env": env}


def test_a_new_user_gets_their_own_sheet_made_for_them(new_user):
    """Signing in is the only step; the spreadsheet and its tabs follow.

    Before this, the first thing a new user had to do was create a
    spreadsheet by hand and copy an id out of its URL.
    """
    client = create_app(_first_run_config()).test_client()

    body = client.post("/api/create-sheet").get_json()

    assert body["ok"] is True
    assert body["url"].endswith("/new-sheet-id/edit")
    # In their own account: the credentials the endpoint used are the ones
    # the sign-in returned, not a shared key from anywhere.
    assert _FakeNewSheets.made == ["Outreach -- hair salon in Penang"]


def test_the_new_sheet_gets_every_tab_the_pipeline_writes_to(new_user):
    """An empty spreadsheet is not a usable one -- discovery writes to Leads,
    research to Research, and a missing tab fails mid-run."""
    holder = {}
    real_init = _FakeNewSheets.__init__

    def capture(self, credentials, spreadsheet_id=""):
        real_init(self, credentials, spreadsheet_id)
        holder["client"] = self

    _FakeNewSheets.__init__ = capture
    try:
        client = create_app(_first_run_config()).test_client()
        client.post("/api/create-sheet")
    finally:
        _FakeNewSheets.__init__ = real_init

    assert holder["client"].tabs == schema.ALL_TABS


def test_the_new_sheet_id_is_saved_so_tomorrow_finds_it(new_user):
    """Held only in memory, the Sheet would be lost the moment the dashboard
    closed -- and the CLI would never see it at all."""
    client = create_app(_first_run_config()).test_client()

    client.post("/api/create-sheet")

    assert new_user["written"]["SHEET_ID"][1] == "new-sheet-id"


def test_a_second_press_never_makes_a_second_sheet(new_user):
    """A new Sheet would be empty, and every lead already gathered would
    look lost. Refuse, and say how to do it deliberately."""
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, sheet_id="already"))
    client = create_app(config).test_client()

    response = client.post("/api/create-sheet")

    assert response.status_code == 400
    assert "already" in response.get_json()["error"].lower()
    assert _FakeNewSheets.made == []


def test_an_abandoned_google_sign_in_is_reported_not_waited_on(new_user,
                                                              monkeypatch):
    """Consent opens a browser and blocks. Someone who closes that tab would
    otherwise park this request thread on a callback that never arrives."""
    from outreach import auth

    def refuse(*a, **kw):
        assert kw.get("consent_timeout"), "consent must be bounded here"
        raise auth.AuthError("The Google sign-in was never finished")

    monkeypatch.setattr(auth, "get_credentials", refuse)
    client = create_app(_first_run_config()).test_client()

    response = client.post("/api/create-sheet")

    assert response.status_code == 400
    assert "never finished" in response.get_json()["error"]
    assert _FakeNewSheets.made == []


def test_a_sheet_that_could_not_be_saved_still_tells_them_where_it_is(
    new_user, monkeypatch
):
    """The spreadsheet exists by then. A bare failure message would leave an
    orphan in their Drive and no way back to it."""
    from outreach import env_edit

    def fail(path, name, value):
        raise env_edit.EnvEditError("Could not write .env: read-only")

    monkeypatch.setattr(env_edit, "set_key", fail)
    client = create_app(_first_run_config()).test_client()

    error = client.post("/api/create-sheet").get_json()["error"]

    assert "new-sheet-id" in error, "the id and URL must survive the failure"


def test_the_first_run_card_offers_to_make_the_sheet(client):
    """A stage Lyon cannot reach from the dashboard does not exist, and the
    same is true of a step a new user cannot reach from the first screen."""
    page = client.get("/classic").get_data(as_text=True)

    assert "createSheet()" in page
    assert "/api/create-sheet" in page


def test_a_failed_reload_still_stops_a_second_sheet(new_user, monkeypatch):
    """The reload after creating is allowed to fail -- the Sheet exists and
    .env is written by then, which is what matters. But the guard must not
    then wave a second press through and make a second, empty Sheet."""
    monkeypatch.setattr(
        dash, "load_config",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("campaign file vanished")),
    )
    app = create_app(_first_run_config()).test_client()

    assert app.post("/api/create-sheet").get_json()["ok"] is True
    second = app.post("/api/create-sheet")

    assert second.status_code == 400
    assert _FakeNewSheets.made == ["Outreach -- hair salon in Penang"]


def test_two_presses_at_once_only_ever_make_one_sheet(new_user, monkeypatch):
    """Flask serves these on separate threads, and the guard that refuses a
    second Sheet is checked *before* a consent flow that blocks for minutes.
    Two presses inside that window would both walk past it."""
    from outreach import auth

    started = threading.Event()
    release = threading.Event()

    def slow_consent(*a, **kw):
        started.set()
        release.wait(5)
        return "creds"

    monkeypatch.setattr(auth, "get_credentials", slow_consent)
    app = create_app(_first_run_config())

    first = []
    thread = threading.Thread(
        target=lambda: first.append(app.test_client().post("/api/create-sheet"))
    )
    thread.start()
    assert started.wait(5), "the first press should have reached consent"

    second = app.test_client().post("/api/create-sheet")
    release.set()
    thread.join(5)

    assert second.status_code == 409
    assert first[0].get_json()["ok"] is True
    assert _FakeNewSheets.made == ["Outreach -- hair salon in Penang"]


# ------------------------------- WhatsApp: log out and pair another phone


def _wa_config(enabled=True):
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    return replace(
        config,
        outreach=replace(config.outreach, whatsapp_enabled=enabled),
        secrets=replace(config.secrets, sheet_id="s"),
    )


def test_resetting_whatsapp_asks_the_relay_to_log_out(monkeypatch):
    """The relay has had a /logout endpoint for months and nothing ever
    called it -- no client method, no dashboard button. A relay endpoint the
    dashboard cannot reach may as well not exist."""
    from outreach.providers import whatsapp as wa

    called = []

    class FakeClient:
        def __init__(self, port=4099):
            called.append(port)

        def logout(self):
            return "Logged out. A new QR code will appear shortly."

    monkeypatch.setattr(wa, "WhatsAppClient", FakeClient)
    client = create_app(_wa_config()).test_client()

    body = client.post("/api/whatsapp/reset").get_json()

    assert body["ok"] is True
    assert "QR" in body["note"]
    assert called == [4099]


def test_a_relay_that_cannot_reset_says_so_instead_of_reporting_success(
    monkeypatch
):
    """If the saved session survives, the relay reconnects to the same
    account. Reporting ok there gives the user a button that appears to work
    and changes nothing."""
    from outreach.providers import whatsapp as wa

    class FakeClient:
        def __init__(self, port=4099):
            pass

        def logout(self):
            raise wa.WhatsAppError("Could not delete the saved WhatsApp session")

    monkeypatch.setattr(wa, "WhatsAppClient", FakeClient)
    client = create_app(_wa_config()).test_client()

    body = client.post("/api/whatsapp/reset").get_json()

    assert "ok" not in body
    assert "Could not delete" in body["error"]


def test_a_relay_that_is_not_running_never_crashes_the_panel(monkeypatch):
    """Every other WhatsApp panel survives a dead relay; this one must too."""
    from outreach.providers import whatsapp as wa

    class FakeClient:
        def __init__(self, port=4099):
            pass

        def logout(self):
            raise OSError("connection refused")

    monkeypatch.setattr(wa, "WhatsAppClient", FakeClient)
    client = create_app(_wa_config()).test_client()

    response = client.post("/api/whatsapp/reset")

    assert response.status_code == 200
    assert "OSError" in response.get_json()["error"]


def test_reset_is_refused_when_whatsapp_is_switched_off():
    client = create_app(_wa_config(enabled=False)).test_client()

    response = client.post("/api/whatsapp/reset")

    assert response.status_code == 400
    assert "switched off" in response.get_json()["error"]


def test_the_send_section_offers_a_way_to_pair_another_phone(client):
    """The button has to be on the page Lyon actually uses. A reset he can
    only reach with curl is not a reset."""
    page = client.get("/classic").get_data(as_text=True)

    # The status-row button specifically. "resetWhatsApp()" on its own
    # matches the function's definition *and* the Yes-log-out button inside
    # its own confirmation, so it passes with the real button unwired --
    # which is exactly what it did before this assertion was tightened.
    assert 'id="wa-reset-btn" onclick="resetWhatsApp()"' in page, (
        "the button in the WhatsApp panel must call the reset"
    )
    assert "async function resetWhatsApp()" in page
    assert "/api/whatsapp/reset" in page
    assert "Log out / switch account" in page
    # And a confirmation, because it sits beside the Send buttons.
    assert "cancelWhatsAppReset" in page


# ------------------------- sending one again without touching the Sheet


class _GuardedSheets:
    """A fake that refuses a manual-column write exactly as the real client does.

    A FakeSheets more permissive than SheetsClient is how the manual-column
    bug shipped past a green suite, so this calls the real guard rather than
    recording every write happily.
    """

    url = "https://sheet"

    def __init__(self, outreach=None, leads=None, briefs=None):
        self._outreach = outreach or {}
        self._leads = leads or {}
        self._briefs = briefs or {}
        self.writes = []

    def read_rows(self, tab):
        if tab is schema.OUTREACH_LOG:
            return self._outreach
        if tab is schema.LEADS:
            return self._leads
        if tab is schema.SITE_BRIEFS:
            return self._briefs
        return {}

    def update_column(self, tab, column, values, *, user_edit=False):
        from outreach.sheets import guard_manual_column

        guard_manual_column(tab, column, user_edit=user_edit)
        self.writes.append((column, dict(values)))
        for key, value in values.items():
            if key in self._outreach:
                self._outreach[key][1][column] = value
        return len(values)


def _sent_row(**over):
    row = {
        "Lead ID": "a",
        "Business Name": "Kedai Kopi",
        "Channel": "WhatsApp",
        "Status": "Drafted",
        "WhatsApp Message": "hello",
        "Drafted At": "2026-09-01T00:00:00Z",
        "WhatsApp Status": "sent",
        "WhatsApp Sent At": "2026-09-02T00:00:00Z",
    }
    row.update(over)
    return {"a": (2, row)}


def _wa_client(monkeypatch, sheets):
    monkeypatch.setattr(dash, "_sheets_client", lambda config: sheets)
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, sheet_id="s"))
    return create_app(config).test_client()


def test_sending_again_clears_what_was_holding_the_lead_back(monkeypatch):
    """A message can be acknowledged by WhatsApp and never arrive -- the
    "Waiting for this message" ones were exactly that. The Sheet then says
    sent when it is not, and the only fix was to open the Sheet by hand."""
    sheets = _GuardedSheets(outreach=_sent_row())
    client = _wa_client(monkeypatch, sheets)

    body = client.post("/api/whatsapp/undo-send", json={"lead_id": "a"}).get_json()

    assert body["ok"] is True
    written = {column: values["a"] for column, values in sheets.writes}
    assert written["WhatsApp Status"] == ""
    assert written["WhatsApp Sent At"] == ""


def test_the_lead_comes_back_into_the_send_queue(monkeypatch):
    """The real test of an undo: the queue offers it again afterwards.
    Asserting the cells were cleared proves a write, not a working button."""
    sheets = _GuardedSheets(
        outreach=_sent_row(),
        leads={"a": (2, {"Lead ID": "a", "Business Name": "Kedai Kopi",
                         "Phone": "+60123456789", "WhatsApp Reachable": "yes"})},
    )
    client = _wa_client(monkeypatch, sheets)

    before = client.get("/api/whatsapp/queue").get_json()
    assert [e["reason"] for e in before["excluded"]] == ["already_sent"]
    assert before["queue"] == []

    client.post("/api/whatsapp/undo-send", json={"lead_id": "a"})
    after = client.get("/api/whatsapp/queue").get_json()

    assert [m["lead_id"] for m in after["queue"]] == ["a"]
    assert after["excluded"] == []


def test_the_message_itself_is_never_touched(monkeypatch):
    """Pressing Send afterwards must send the same message, not a new one --
    the draft and its screenshot cost money to produce."""
    sheets = _GuardedSheets(outreach=_sent_row())
    client = _wa_client(monkeypatch, sheets)

    client.post("/api/whatsapp/undo-send", json={"lead_id": "a"})

    touched = {column for column, _ in sheets.writes}
    assert "WhatsApp Message" not in touched
    assert "Drafted At" not in touched


def test_a_hand_edited_status_is_reset_only_when_it_is_the_blocker(monkeypatch):
    """Status is a manual column, so a stage may never write it. This is the
    exception the rule names -- one named row the user just clicked."""
    sheets = _GuardedSheets(outreach=_sent_row(Status="Sent"))
    client = _wa_client(monkeypatch, sheets)

    client.post("/api/whatsapp/undo-send", json={"lead_id": "a"})

    assert ("Status", {"a": "Drafted"}) in sheets.writes


def test_a_status_already_saying_drafted_is_left_alone(monkeypatch):
    """Writing a manual column that is already right is still writing one."""
    sheets = _GuardedSheets(outreach=_sent_row(Status="Drafted"))
    client = _wa_client(monkeypatch, sheets)

    client.post("/api/whatsapp/undo-send", json={"lead_id": "a"})

    assert "Status" not in {column for column, _ in sheets.writes}


def test_sending_again_needs_a_drafted_message_to_send(monkeypatch):
    sheets = _GuardedSheets(outreach={})
    client = _wa_client(monkeypatch, sheets)

    body = client.post("/api/whatsapp/undo-send", json={"lead_id": "ghost"}).get_json()

    assert "nothing to send again" in body["error"]
    assert sheets.writes == []


def test_the_send_panel_offers_send_again_only_where_it_would_work(client):
    """No phone, no Leads row and drafted-as-email each need a different fix.
    A button promising to undo them would simply not work."""
    page = client.get("/classic").get_data(as_text=True)

    assert 'onclick="undoSend(' in page
    assert "/api/whatsapp/undo-send" in page
    assert "const UNDOABLE = {already_sent: 1, status_not_drafted: 1}" in page


# --------------------- removing a lead that is not on WhatsApp


def test_the_not_on_whatsapp_list_can_disqualify_a_lead(client):
    """Answered dead ends. Removing one stops it being researched or built."""
    page = client.get("/classic").get_data(as_text=True)

    assert 'onclick="dropUnreachable(' in page
    assert "async function dropUnreachable(" in page
    # The rejected list gets the button; the verified one must not.
    assert "fold('Not on WhatsApp', d.rejected || [], true)" in page
    assert "fold('On WhatsApp', d.verified || [])" in page


def test_removing_a_lead_warns_when_it_still_has_an_email(client):
    """A lead with no WhatsApp but a known email is still buildable -- it is
    counted in the panel's own "can be followed up" number."""
    page = client.get("/classic").get_data(as_text=True)

    assert "Remove anyway?" in page
    assert "could still be contacted that way" in page


# ------------------------------------------- the art-direction switch


def test_the_design_level_is_readable_from_the_page(client):
    body = client.get("/api/design-level").get_json()

    assert body["level"] in ("standard", "showcase")
    assert body["levels"] == ["showcase", "standard"]


def test_the_switch_is_on_the_page_lyon_uses(client):
    """A setting he can only reach by editing YAML is a setting he does not
    have -- the same rule that made the reachability stage a button."""
    page = client.get("/classic").get_data(as_text=True)

    assert 'id="design-showcase"' in page
    assert "setDesignLevel(this.checked)" in page
    assert "/api/design-level" in page


def test_an_unknown_level_is_refused_without_touching_the_campaign(client):
    response = client.post("/api/design-level", json={"level": "fancy"})

    assert response.status_code == 400
    assert "fancy" in response.get_json()["error"]


def test_setting_the_level_forks_to_the_local_campaign(campaign_app):
    """Never write to a git-tracked file at runtime -- the dashboard once
    broke `git pull` on Lyon's machine doing exactly this."""
    client, local = campaign_app
    tracked = Path("campaign.yaml")
    before = tracked.read_text()

    body = client.post("/api/design-level", json={"level": "standard"}).get_json()

    assert body["ok"] is True
    assert tracked.read_text() == before, "the tracked campaign file was written to"
    assert 'design_level: "standard"' in local.read_text()


def test_the_new_level_takes_effect_without_a_restart(campaign_app):
    client, _ = campaign_app

    client.post("/api/design-level", json={"level": "standard"})

    assert client.get("/api/design-level").get_json()["level"] == "standard"


def test_saving_the_level_keeps_the_campaign_files_comments(campaign_app):
    client, local = campaign_app
    before = Path("campaign.yaml").read_text().count("#")

    client.post("/api/design-level", json={"level": "standard"})

    assert local.read_text().count("#") == before


def test_an_unconfirmed_send_leaves_the_queue(monkeypatch):
    """The bug that kept a sent message at the top of the send list.

    WhatsApp confirms delivery late often enough -- a first message to a new
    contact especially -- that the relay reports `unconfirmed` whenever no ack
    arrives in its window. That means the message left. Treating it as
    still-to-send offered the same real business the same message again, with
    nothing on the page to say it had already gone.
    """
    sheets = _GuardedSheets(
        outreach=_sent_row(**{"WhatsApp Status": "unconfirmed"}),
        leads={"a": (2, {"Lead ID": "a", "Business Name": "Kedai Kopi",
                         "Phone": "+60123456789", "WhatsApp Reachable": "yes"})},
    )
    client = _wa_client(monkeypatch, sheets)

    body = client.get("/api/whatsapp/queue").get_json()

    assert body["queue"] == [], "an unconfirmed message must not be offered again"
    assert [e["reason"] for e in body["excluded"]] == ["already_sent"]


def test_an_unconfirmed_send_is_not_counted_as_still_waiting(monkeypatch):
    """The count at the top and the queue below must agree -- a count that
    disagrees with the list makes the page look broken."""
    rows = _sent_row(**{"WhatsApp Status": "unconfirmed"})
    summary = dash.summarise({}, set(), set(), rows, {})

    assert summary.whatsapp_waiting == 0


def test_a_failed_send_is_still_offered(monkeypatch):
    """The opposite case. `failed` means it did not go, so it stays."""
    sheets = _GuardedSheets(
        outreach=_sent_row(**{"WhatsApp Status": "failed"}),
        leads={"a": (2, {"Lead ID": "a", "Business Name": "Kedai Kopi",
                         "Phone": "+60123456789", "WhatsApp Reachable": "yes"})},
    )
    client = _wa_client(monkeypatch, sheets)

    body = client.get("/api/whatsapp/queue").get_json()

    assert [m["lead_id"] for m in body["queue"]] == ["a"]


# --------------- the send panel's state machine, actually executed


def _page_script() -> str:
    """The rendered page's single <script>, ready to run under node."""
    import re as _re

    from flask import Flask, render_template_string

    from outreach.dashboard import PAGE

    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    app = Flask(__name__)
    with app.app_context():
        page = render_template_string(
            PAGE,
            campaign=config.campaign,
            build=config.build,
            whatsapp_enabled=config.outreach.whatsapp_enabled,
        )
    blocks = _re.findall(r"<script>(.*?)</script>", page, _re.S)
    assert blocks, "the page has no script block"
    return max(blocks, key=len)


def _run_in_node(harness: str) -> str:
    """Run the page's script under node with a stub DOM, then the harness.

    Parsing the page proved it was syntactically valid while the send flow was
    still broken. These two bugs were both about state being discarded between
    renders, which only executing it can show.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")

    stub = """
    const els = {};
    function fakeEl() {
      return {innerHTML: '', textContent: '', value: '', disabled: false,
              classList: {add(){}, remove(){}}, style: {},
              querySelector: () => fakeEl(), hidden: false};
    }
    globalThis.document = {
      querySelector: () => fakeEl(),
      querySelectorAll: () => [],
      getElementById: () => fakeEl(),
      addEventListener(){},
    };
    globalThis.window = globalThis;
    globalThis.setInterval = () => 0;
    globalThis.setTimeout = (f) => { return 0; };
    // Rejects by default so the page's own start-up calls all take their
    // error paths and bail out. Each harness installs the fetch it needs
    // afterwards; satisfying every panel's response shape here would be a
    // second copy of the server, wrong the moment either changed.
    globalThis.fetch = async () => { throw new Error('no network in this test'); };
    process.on('unhandledRejection', () => {});
    """
    script = _page_script()
    # The page's own bootstrap calls would fire network stubs and redraw; the
    # harness drives the functions directly instead.
    src = stub + "\n" + script + "\n" + harness
    proc = subprocess.run(
        [node, "--input-type=module", "-e", src],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


def test_a_refresh_between_the_two_presses_does_not_lose_the_confirmation():
    """Issue reported from a real run: "I clicked sending twice but it still
    didn't go out at all."

    The first press arms a confirmation; the second sends. The queue is
    rebuilt from the server every 30 seconds, and the flag used to live on
    the queue object -- so a refresh landing between the presses discarded
    it, and the second press only armed it again.
    """
    out = _run_in_node("""
    waQueue = [{lead_id: 'a', name: 'Kedai', phone: '+60123', message: 'hi'}];
    let sent = 0;
    globalThis.fetch = async (url) => {
      if (url === '/api/whatsapp/send') sent += 1;
      return {json: async () => ({ok: true})};
    };
    globalThis.loadWhatsAppInline = async () => {};
    globalThis.refresh = async () => {};

    sendWhatsApp('a');                       // press one: arm
    waQueue = [{lead_id: 'a', name: 'Kedai', phone: '+60123', message: 'hi'}];
    sendWhatsApp('a');                       // press two: must send
    console.log(JSON.stringify({sent}));
    """)
    assert '"sent":1' in out, (
        "the second press did not send -- the confirmation was lost when the "
        "queue was replaced"
    )


def test_the_sent_state_survives_the_redraw_that_follows_it():
    """The other half of the same bug: "it doesn't move it back down to the
    sent column". The success flag was set on a queue object the redraw had
    already replaced, so the card rendered as unsent again."""
    out = _run_in_node("""
    waQueue = [{lead_id: 'a', name: 'K', phone: '+60123', message: 'hi'}];
    globalThis.fetch = async () => ({json: async () => ({ok: true, note: ''})});
    globalThis.loadWhatsAppInline = async () => {
      // What the real one does: fresh objects from the server.
      waQueue = [{lead_id: 'a', name: 'K', phone: '+60123', message: 'hi'}];
    };
    globalThis.refresh = async () => {};

    sendWhatsApp('a');
    sendWhatsApp('a');
    await new Promise(r => process.nextTick(r));
    await new Promise(r => process.nextTick(r));
    console.log(JSON.stringify({sent: !!waUi['a'].sent}));
    """)
    assert '"sent":true' in out


def test_an_edit_is_not_wiped_by_the_background_refresh():
    """The timer stands aside while a card is being typed into. Losing an
    edited message to a 30-second tick is silent and unrecoverable."""
    out = _run_in_node("""
    noteEdit('a', 'my edited message');
    console.log(JSON.stringify({busy: waBusy(), draft: waUi['a'].draft}));
    """)
    assert '"busy":true' in out
    assert "my edited message" in out


def test_the_edited_text_is_what_gets_sent():
    out = _run_in_node("""
    waQueue = [{lead_id: 'a', name: 'K', phone: '+60123', message: 'original'}];
    let body = null;
    globalThis.fetch = async (url, opts) => {
      body = JSON.parse(opts.body);
      return {json: async () => ({ok: true})};
    };
    globalThis.loadWhatsAppInline = async () => {};
    globalThis.refresh = async () => {};

    noteEdit('a', 'edited by hand');
    sendWhatsApp('a');
    sendWhatsApp('a');
    await new Promise(r => process.nextTick(r));
    console.log(JSON.stringify({message: body.message}));
    """)
    assert "edited by hand" in out


def test_a_lead_is_addressed_by_id_never_by_its_position():
    """The queue is rebuilt on a timer and a card's index moves the moment
    anything enters or leaves the list. An index captured when the button was
    drawn could address a different business by the time it was pressed."""
    out = _run_in_node("""
    waQueue = [
      {lead_id: 'first', name: 'A', phone: '+601', message: 'a'},
      {lead_id: 'second', name: 'B', phone: '+602', message: 'b'},
    ];
    let addressed = null;
    globalThis.fetch = async (url, opts) => {
      addressed = JSON.parse(opts.body).lead_id;
      return {json: async () => ({ok: true})};
    };
    globalThis.loadWhatsAppInline = async () => {};
    globalThis.refresh = async () => {};

    sendWhatsApp('second');                  // arm the second card
    waQueue.shift();                         // the first one leaves the queue
    sendWhatsApp('second');                  // still the same business
    await new Promise(r => process.nextTick(r));
    console.log(JSON.stringify({addressed}));
    """)
    assert '"addressed":"second"' in out


def test_no_two_page_functions_share_a_name():
    """A second function of the same name silently replaces the first.

    This shipped: a `dropLead` added for the WhatsApp-check panel replaced
    the one the lead preview's "not this one" button calls, with a different
    signature -- so that button started passing an index where a lead ID was
    expected. Nothing failed, because the page still parsed.
    """
    import re as _re

    names = _re.findall(
        r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
        _page_script(),
        _re.M,
    )
    seen, dupes = set(), set()
    for name in names:
        if name in seen:
            dupes.add(name)
        seen.add(name)

    assert not dupes, f"declared more than once on the page: {sorted(dupes)}"


# ------------------------------- not this one: skipping without sending


def test_skipping_takes_a_message_off_the_list_without_sending(monkeypatch):
    """"Not this one" -- the answer that previously meant either sending it
    anyway or editing the Sheet by hand."""
    sheets = _GuardedSheets(
        outreach=_sent_row(**{"WhatsApp Status": "", "WhatsApp Sent At": ""}),
        leads={"a": (2, {"Lead ID": "a", "Business Name": "Kedai Kopi",
                         "Phone": "+60123456789", "WhatsApp Reachable": "yes"})},
    )
    client = _wa_client(monkeypatch, sheets)
    assert [m["lead_id"] for m in client.get("/api/whatsapp/queue").get_json()["queue"]] == ["a"]

    body = client.post("/api/whatsapp/skip", json={"lead_id": "a"}).get_json()

    assert body["ok"] is True
    after = client.get("/api/whatsapp/queue").get_json()
    assert after["queue"] == []
    assert [e["reason"] for e in after["excluded"]] == ["status_not_drafted"]


def test_skipping_keeps_the_message_and_the_screenshot(monkeypatch):
    """It is not a disqualification. Everything that cost money to produce
    stays, so Send again is a single press rather than a rebuild."""
    sheets = _GuardedSheets(
        outreach=_sent_row(**{"WhatsApp Status": "", "WhatsApp Sent At": ""}),
    )
    client = _wa_client(monkeypatch, sheets)

    client.post("/api/whatsapp/skip", json={"lead_id": "a"})

    touched = {column for column, _ in sheets.writes}
    assert touched == {"Status"}, f"skip wrote more than the Status cell: {touched}"
    assert sheets._outreach["a"][1]["WhatsApp Message"] == "hello"


def test_a_skipped_message_can_be_sent_again(monkeypatch):
    """The undo is why skipping does not stop to confirm."""
    sheets = _GuardedSheets(
        outreach=_sent_row(**{"WhatsApp Status": "", "WhatsApp Sent At": ""}),
        leads={"a": (2, {"Lead ID": "a", "Business Name": "Kedai Kopi",
                         "Phone": "+60123456789", "WhatsApp Reachable": "yes"})},
    )
    client = _wa_client(monkeypatch, sheets)
    client.post("/api/whatsapp/skip", json={"lead_id": "a"})

    client.post("/api/whatsapp/undo-send", json={"lead_id": "a"})

    queue = client.get("/api/whatsapp/queue").get_json()["queue"]
    assert [m["lead_id"] for m in queue] == ["a"]


def test_skipping_a_lead_with_no_draft_says_so(monkeypatch):
    sheets = _GuardedSheets(outreach={})
    client = _wa_client(monkeypatch, sheets)

    body = client.post("/api/whatsapp/skip", json={"lead_id": "ghost"}).get_json()

    assert "nothing to skip" in body["error"]
    assert sheets.writes == []


def test_the_skip_button_sits_beside_send_and_beside_retry(client):
    """Both states need it: a message you do not want to send and a message
    that just failed are the same decision."""
    page = client.get("/classic").get_data(as_text=True)

    assert "skipWhatsApp('${esc(m.lead_id)}')" in page
    assert "/api/whatsapp/skip" in page
    # Once in the failed branch, once in the normal one.
    assert page.count('class="wa-skip"') == 2


# ------------------------------------- the services list, from the page


def test_the_services_are_readable_from_the_page(client):
    body = client.get("/api/services").get_json()

    assert isinstance(body["services"], list)
    assert "intro" in body


def test_the_page_offers_a_place_to_edit_them(client):
    """The wording is the part worth tuning after reading a few replies, and
    a setting reachable only by editing YAML is a setting Lyon does not have."""
    page = client.get("/classic").get_data(as_text=True)

    assert 'id="svc-intro"' in page
    assert 'id="svc-list"' in page
    assert "saveServices()" in page
    assert "/api/services" in page


def test_saving_services_forks_to_the_local_campaign(campaign_app):
    client, local = campaign_app
    tracked = Path("campaign.yaml")
    before = tracked.read_text()

    body = client.post("/api/services", json={
        "intro": "Other ways I help:",
        "services": ["Google Business Profile", "Marketing funnels"],
    }).get_json()

    assert body["ok"] is True
    assert tracked.read_text() == before, "the tracked campaign file was written to"
    saved = local.read_text()
    assert "Other ways I help:" in saved
    assert "Marketing funnels" in saved


def test_the_new_services_take_effect_without_a_restart(campaign_app):
    client, _ = campaign_app

    client.post("/api/services", json={
        "intro": "Other ways I help:", "services": ["One", "Two"],
    })

    assert client.get("/api/services").get_json()["services"] == ["One", "Two"]


def test_services_can_be_cleared_from_the_page(campaign_app):
    """Turning the feature off must be as reachable as turning it on."""
    client, _ = campaign_app
    client.post("/api/services", json={"intro": "x:", "services": ["One"]})

    client.post("/api/services", json={"intro": "", "services": []})

    assert client.get("/api/services").get_json()["services"] == []


def test_a_list_with_no_intro_is_refused_and_nothing_is_written(campaign_app):
    client, local = campaign_app
    before = local.read_text() if local.exists() else None

    body = client.post("/api/services", json={
        "intro": "  ", "services": ["One"],
    }).get_json()

    assert "introduces" in body["error"]
    after = local.read_text() if local.exists() else None
    assert after == before


def test_saving_services_keeps_the_campaign_files_comments(campaign_app):
    client, local = campaign_app
    before = Path("campaign.yaml").read_text().count("#")

    client.post("/api/services", json={"intro": "x:", "services": ["One"]})

    assert local.read_text().count("#") == before


# ------------------------------------------------- signing in from a phone


def _locked_app(password="hunter2"):
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(
        config, secrets=replace(config.secrets, dashboard_password=password)
    )
    return create_app(config).test_client()


def test_with_no_password_set_nothing_asks_you_to_sign_in(client):
    """The desktop workflow is unchanged. A login nobody needs is a login
    nobody will set a password for."""
    assert client.get("/classic").status_code == 200
    assert client.get("/api/summary").status_code == 200


def test_a_password_locks_the_page_and_the_api():
    app = _locked_app()

    page = app.get("/classic")
    api = app.get("/api/summary")

    assert page.status_code == 302 and "/login" in page.headers["Location"]
    # JSON for the API: a browser handed a login page as an API response
    # renders raw HTML into the panel, and a phone shows nothing at all.
    assert api.status_code == 401
    assert "Signed out" in api.get_json()["error"]


def test_the_right_password_gets_you_in_and_stays_in():
    app = _locked_app()

    response = app.post("/login", data={"password": "hunter2"})

    assert response.status_code == 302
    assert app.get("/classic").status_code == 200
    assert app.get("/api/summary").status_code == 200


def test_the_wrong_password_does_not():
    app = _locked_app()

    response = app.post("/login", data={"password": "wrong"})

    assert response.status_code == 401
    assert b"Wrong password" in response.data
    assert app.get("/api/summary").status_code == 401


def test_guessing_is_rate_limited():
    """Anything else on the wifi could otherwise try passwords at machine
    speed."""
    app = _locked_app()

    for _ in range(8):
        app.post("/login", data={"password": "wrong"})
    blocked = app.post("/login", data={"password": "wrong"})

    assert b"Too many attempts" in blocked.data
    # And the real password is refused too while the window is open, so the
    # limit cannot be stepped around by getting it right on the ninth try.
    assert b"Too many attempts" in app.post(
        "/login", data={"password": "hunter2"}
    ).data


def test_the_login_page_never_says_what_the_password_is():
    """Secrets are write-only -- never echoed to a page, a log or a report."""
    app = _locked_app(password="a-very-distinctive-secret")

    for response in (app.get("/login"), app.post("/login", data={"password": "x"})):
        assert b"a-very-distinctive-secret" not in response.data


# ------------------------------------------- installing it on a phone


def test_the_page_can_be_installed_as_an_app(client):
    """Add to Home Screen gives an icon that opens without Safari's chrome.
    Without `standalone` it is a bookmark with extra steps."""
    manifest = client.get("/manifest.webmanifest").get_json()

    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest["icons"], "an installed app with no icon shows a screenshot"


def test_the_icon_is_a_real_png(client):
    """Generated rather than shipped, so it cannot drift from the palette --
    but it still has to be a file iOS will accept."""
    import struct

    response = client.get("/icon.png")

    assert response.mimetype == "image/png"
    assert response.data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", response.data[16:24])
    assert (width, height) == (512, 512), "iOS wants 512 for the home screen"


def test_the_page_links_everything_ios_needs(client):
    page = client.get("/classic").get_data(as_text=True)

    assert 'rel="manifest"' in page
    assert 'rel="apple-touch-icon"' in page
    assert 'name="apple-mobile-web-app-capable" content="yes"' in page
    # viewport-fit=cover, or the page stops short of the screen edges and the
    # safe-area padding below has nothing to do.
    assert "viewport-fit=cover" in page


def test_inputs_are_big_enough_that_safari_does_not_zoom(client):
    """Below 16px Safari zooms the page in on focus and never zooms back --
    tapping the message box to fix a typo left it magnified."""
    page = client.get("/classic").get_data(as_text=True)

    phone = page.split("@media (max-width:640px)", 1)[1].split("}\n  }", 1)[0]
    assert "font-size:16px" in phone
    assert "input, textarea" in phone


def test_the_phone_layout_keeps_content_out_of_the_island_and_the_indicator(client):
    page = client.get("/classic").get_data(as_text=True)

    phone = page.split("@media (max-width:640px)", 1)[1].split("}\n  }", 1)[0]
    assert "env(safe-area-inset-top)" in phone
    assert "env(safe-area-inset-bottom)" in phone


def test_the_send_buttons_are_thumb_sized_on_a_phone(client):
    """These send messages to real businesses. 44px is Apple's own minimum."""
    page = client.get("/classic").get_data(as_text=True)

    phone = page.split("@media (max-width:640px)", 1)[1].split("}\n  }", 1)[0]
    assert "min-height:44px" in phone


def test_the_login_page_is_phone_shaped_too():
    """It is the first thing seen on the phone, and the only page you cannot
    get past -- a login that zooms on focus is a bad first impression."""
    app = _locked_app()

    page = app.get("/login").get_data(as_text=True)

    assert "viewport-fit=cover" in page
    assert "font-size:16px" in page
    assert "env(safe-area-inset-bottom)" in page


def test_an_error_with_steps_in_it_keeps_its_line_breaks():
    """The expired-sign-in message is three numbered steps and a URL.

    It reached the page inside a `<span>`, where HTML collapses every
    newline, so the steps ran together into one paragraph on the only screen
    he reads them from -- guidance written and then flattened, which is the
    same as not writing it.

    Driven through the page's own refresh rather than read off the source:
    what matters is the element the error lands in, not the line that put it
    there.
    """
    out = _run_in_node("""
    const box = fakeEl();
    document.querySelector = (s) => (s === '#totals' ? box : fakeEl());
    globalThis.fetch = async (url) => {
      if (url === '/api/summary') {
        return {json: async () => ({error: 'Step one.\\nStep two.\\nStep three.'})};
      }
      throw new Error('no network in this test');
    };
    await refresh();
    console.log(JSON.stringify({html: box.innerHTML}));
    """)

    # The newlines survive into the markup, and the element preserves them.
    assert "Step one.\nStep two." in out.replace("\\n", "\n")
    assert "long-err" in out

    from outreach.dashboard import PAGE

    assert "white-space:pre-wrap" in PAGE, (
        "the class is applied but nothing makes it keep the breaks"
    )


# --------------------------------------------- the infographic panel, running
#
# Parsing the page is not running it, and the two send bugs that cost a
# morning were both invisible to `node --check`. So the infographic panel's
# state machine is driven under node too, with the HTML it produces read back.


_VIZ_DOM = """
    const _registry = {};
    function _el(sel) {
      if (!_registry[sel]) _registry[sel] = {
        innerHTML: '', textContent: '', value: '', className: '',
        disabled: false, hidden: false, style: {},
        classList: {add(){}, remove(){}},
        select(){}, focus(){}, querySelector: () => null,
      };
      return _registry[sel];
    }
    globalThis._el = _el;
    globalThis.document = {
      querySelector: (sel) => _el(sel),
      querySelectorAll: () => [],
      getElementById: (id) => _el('#' + id),
      addEventListener(){},
    };
    globalThis.refresh = async () => {};
"""


def _viz_queue_response(**over):
    import json as _json

    payload = {
        "enabled": True,
        "track": "visual",
        "tool_name": "Google Flow",
        "tool_url": "",
        "signature": "by Lyon",
        "counts": {"needs_image": 1, "needs_check": 0, "ready": 0, "skipped": 0},
        "catalogue": {"styles": [], "layouts": [], "chosen_styles": [],
                      "chosen_layouts": []},
        "queue": [
            {
                "lead_id": "LEAD-1",
                "name": "Your Nail Crush",
                "category": "Nail salon",
                "prompt": "Wide 16:9 ... for \"Your Nail Crush\".",
                "gaps": "no_website: Google lists no website",
                "style": "paper-cut",
                "layout": "rows",
                "specific": True,
                "state": "needs_image",
                "approval": "Approved",
                "expected_text": ["3 quick wins for Your Nail Crush",
                                  "Calls go unanswered  ->  Let them book online"],
                "created_at": "2026-01-01T00:00:00Z",
            }
        ],
    }
    payload.update(over)
    return _json.dumps(payload)


def test_the_infographic_panel_renders_a_card_per_prompt():
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_viz_queue_response()})}});
    await loadVisual();
    vizToggle('LEAD-1');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "Your Nail Crush" in html
    assert "Copy prompt" in html
    assert "paper-cut" in html


def test_a_card_waiting_for_a_picture_offers_all_three_ways_in():
    """He has the link and the downloadable file, and paste is the natural
    gesture -- so none of the three is the only one."""
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_viz_queue_response()})}});
    await loadVisual();
    vizToggle('LEAD-1');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "Ctrl+V" in html
    assert 'type="file"' in html
    assert "Copy image address" in html


def test_a_picture_that_nobody_checked_asks_for_the_words_to_be_read():
    """Nothing in this tool can test that the text rendered, so the panel has
    to put the exact words next to the picture and make the tick the gate."""
    q = __import__("json").loads(_viz_queue_response())
    q["queue"][0]["state"] = "needs_check"
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({__import__('json').dumps(q)})}});
    await loadVisual();
    vizSetFilter('needs_check');
    vizToggle('LEAD-1');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "The words are right" in html
    assert "3 quick wins for Your Nail Crush" in html
    assert "Calls go unanswered" in html
    assert "<img" in html and "/api/screenshot/" in html


def test_a_checked_picture_says_so_instead_of_asking_again():
    q = __import__("json").loads(_viz_queue_response())
    q["queue"][0]["state"] = "ready"
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({__import__('json').dumps(q)})}});
    await loadVisual();
    vizSetFilter('ready');
    vizToggle('LEAD-1');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "The words are right" not in html
    assert "Picture checked" in html


def test_a_generic_graphic_says_so_on_its_own_card():
    """He has to know which graphics carry his claims and which do not,
    before he sends one."""
    q = __import__("json").loads(_viz_queue_response())
    q["queue"][0]["specific"] = False
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({__import__('json').dumps(q)})}});
    await loadVisual();
    vizToggle('LEAD-1');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "not enough evidence to name them" in html


def test_the_step_is_hidden_when_the_campaign_pitches_a_page():
    """A panel that can never do anything reads as broken."""
    out = _run_in_node(_VIZ_DOM + """
    globalThis.fetch = async () => ({json: async () => ({
      enabled: false, track: 'website', queue: []
    })});
    await loadVisual();
    console.log(JSON.stringify({
      html: _el('#visual').innerHTML,
      step: _el('#visual-step').style.display,
    }));
    """)
    data = __import__("json").loads(out)
    assert data["html"] == ""
    assert data["step"] == "none"


def test_a_lead_is_addressed_by_its_id_and_never_by_its_index():
    """The queue is rebuilt underneath the buttons every 30 seconds."""
    q = __import__("json").loads(_viz_queue_response())
    q["queue"].append({**q["queue"][0], "lead_id": "LEAD-2", "name": "Second"})
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({__import__('json').dumps(q)})}});
    await loadVisual();
    vizToggle('LEAD-1');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    html = __import__("json").loads(out)["html"]
    import re as _re

    assert "'LEAD-2'" in html
    handlers = _re.findall(r"on\w+=\"(\w+)\(([^)]*)\)", html)
    numeric = [h for h in handlers if _re.fullmatch(r"\d+", h[1].strip())]
    assert not numeric, f"a handler is addressed by index: {numeric}"


def test_the_timer_stands_aside_while_a_link_is_being_typed():
    """A refresh landing mid-decision is what discarded the send confirmation
    twice over; the same mistake here would wipe a half-typed link."""
    out = _run_in_node(_VIZ_DOM + """
    console.log(JSON.stringify({idle: vizBusy()}));
    vizArm('LEAD-1', true);
    console.log(JSON.stringify({armed: vizBusy()}));
    vizArm('LEAD-1', false);
    console.log(JSON.stringify({done: vizBusy()}));
    """)
    assert '"idle":false' in out
    assert '"armed":true' in out
    assert '"done":false' in out


def test_pasting_something_that_is_not_a_picture_says_so():
    """"Copy image" and "copy the page" are one keystroke apart."""
    out = _run_in_node(_VIZ_DOM + """
    let posted = 0;
    globalThis.fetch = async () => { posted += 1; return {json: async () => ({ok: true})}; };
    onVizPaste('LEAD-1', {
      clipboardData: {items: [{type: 'text/html', getAsFile: () => null}]},
      preventDefault(){},
    });
    console.log(JSON.stringify({posted, note: vizUi['LEAD-1'].note}));
    """)
    data = __import__("json").loads(out)
    assert data["posted"] == 0, "a non-image must not be uploaded"
    assert "not a picture" in data["note"]


def test_an_error_from_the_server_is_shown_on_the_card_not_swallowed():
    out = _run_in_node(_VIZ_DOM + """
    globalThis.fetch = async () => ({json: async () => ({
      error: 'That image is 12.0MB, over the 8MB limit.'
    })});
    globalThis.loadVisual = async () => {};
    await attachVizImage('LEAD-1', 'data:image/png;base64,aGk=', '');
    console.log(JSON.stringify({note: vizUi['LEAD-1'].note}));
    """)
    assert "over the 8MB limit" in __import__("json").loads(out)["note"]


def test_the_track_selector_is_outside_the_step_it_switches_on():
    """Put inside, the one control that turns graphics on would only be
    visible once they were already on."""
    from outreach.dashboard import PAGE

    page = PAGE
    settings = page.index('id="track-settings"')
    step = page.index('id="visual-step"')
    assert settings < step, "the track selector is rendered inside the step"
    # And it is not in the block the panel hides.
    assert 'id="rotation-settings"' in page


def test_the_track_selector_offers_all_three_and_asks_for_the_signature():
    out = _run_in_node(_VIZ_DOM + """
    globalThis.fetch = async () => ({json: async () => ({
      track: 'both', tracks: ['both','visual','website'],
      signature: 'by Lyon', tool_name: 'Google Flow'
    })});
    await loadTrack();
    console.log(JSON.stringify({
      picked: _el('#track-pick').value,
      sig: _el('#track-sig').value,
      sigShown: _el('#sig-wrap').style.display,
    }));
    """)
    data = __import__("json").loads(out)
    assert data["picked"] == "both"
    assert data["sig"] == "by Lyon"
    assert data["sigShown"] == "block"


def test_choosing_the_website_track_stops_asking_for_a_signature():
    """It is the ribbon on a graphic. A campaign with no graphics has nowhere
    to put it, so asking would be asking for something unused."""
    out = _run_in_node(_VIZ_DOM + """
    _el('#track-pick').value = 'website';
    trackPicked();
    console.log(JSON.stringify({shown: _el('#sig-wrap').style.display}));
    """)
    assert '"shown":"none"' in out


def test_the_rotation_tick_boxes_are_drawn_from_what_the_server_says_exists():
    """A list written out in the page drifts from the one the server will
    accept, and the mismatch only shows as a refused save."""
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_viz_queue_response(
        catalogue={"styles": ["paper-cut", "watercolour", "pop-art"],
                   "layouts": ["rows", "road"],
                   "chosen_styles": ["watercolour"],
                   "chosen_layouts": []}
    )})}});
    await loadVisual();
    console.log(JSON.stringify({{
      styles: _el('#rot-styles').innerHTML,
      layouts: _el('#rot-layouts').innerHTML,
      shown: _el('#rotation-settings').style.display,
    }}));
    """)
    data = __import__("json").loads(out)
    assert "paper-cut" in data["styles"]
    assert "pop-art" in data["styles"]
    assert "road" in data["layouts"]
    # The one already chosen comes back ticked, so saving does not silently
    # clear a narrowing that was already in place.
    assert data["styles"].count("checked") == 1
    assert data["shown"] == ""


def test_the_rotation_box_is_hidden_when_the_campaign_pitches_a_page():
    out = _run_in_node(_VIZ_DOM + """
    globalThis.fetch = async () => ({json: async () => ({
      enabled: false, track: 'website', queue: [],
      catalogue: {styles: ['paper-cut'], layouts: ['rows'],
                  chosen_styles: [], chosen_layouts: []},
    })});
    await loadVisual();
    console.log(JSON.stringify({shown: _el('#rotation-settings').style.display}));
    """)
    assert '"shown":"none"' in out


def test_a_missing_tab_renders_the_button_the_message_tells_him_to_press():
    """The message says "press Add the missing tabs below". Guidance that
    names a button nowhere near it is the same as no guidance."""
    out = _run_in_node(_VIZ_DOM + """
    globalThis.fetch = async () => ({json: async () => ({
      enabled: true, track: 'visual', queue: [],
      error: 'Your Sheet has no \\u201cVisual Briefs\\u201d tab yet.\\nPress the button.',
      missing_tab: true,
    })});
    await loadVisual();
    console.log(JSON.stringify({html: _el('#visual').innerHTML}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "Add the missing tabs" in html
    assert "repairTabs(" in html
    # The message is several lines, and HTML collapses every newline inside a
    # <span> -- which is how a three-step cure once reached the page as one
    # run-on paragraph.
    assert "long-err" in html


def test_an_ordinary_error_offers_no_repair_button():
    """A button that cannot fix what went wrong is worse than no button."""
    out = _run_in_node(_VIZ_DOM + """
    globalThis.fetch = async () => ({json: async () => ({
      enabled: true, track: 'visual', queue: [],
      error: 'SheetsError: the network went away', missing_tab: false,
    })});
    await loadVisual();
    console.log(JSON.stringify({html: _el('#visual').innerHTML}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "the network went away" in html
    assert "Add the missing tabs" not in html


def test_pressing_the_repair_reloads_the_panel_that_was_broken():
    """Repairing and then leaving the error on screen reads as the button
    having done nothing."""
    out = _run_in_node(_VIZ_DOM + """
    const asked = [];
    globalThis.fetch = async (url) => {
      asked.push(url);
      return {json: async () => ({ok: true, added: ['Visual Briefs'],
                                  note: 'Added: Visual Briefs',
                                  enabled: true, queue: [], counts: {}})};
    };
    await repairTabs(null);
    console.log(JSON.stringify({
      posted: asked.filter(u => u === '/api/repair-tabs').length,
      reloaded: asked.filter(u => u === '/api/visual/queue').length,
      note: _el('#repair-note').textContent,
    }));
    """)
    data = __import__("json").loads(out)
    assert data["posted"] == 1
    assert data["reloaded"] == 1
    assert "Added: Visual Briefs" in data["note"]


# ------------------------------------------- a list short enough to work from
#
# Reported after the first real run: sixty cards, every one expanded, with no
# way to narrow them or get one off the screen.


def _many(n, state="needs_image"):
    import json as _json

    base = _json.loads(_viz_queue_response())
    row = base["queue"][0]
    base["queue"] = [
        {**row, "lead_id": f"LEAD-{i}", "name": f"Business {i}", "state": state}
        for i in range(n)
    ]
    base["counts"] = {"needs_image": n, "needs_check": 0, "ready": 0, "skipped": 0}
    return _json.dumps(base)


def test_every_card_starts_collapsed():
    """Sixty expanded cards is not a list anybody reads. The header carries
    what is needed to decide whether to open one."""
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_many(12)})}});
    await loadVisual();
    const html = _el('#visual').innerHTML;
    console.log(JSON.stringify({{
      rows: (html.match(/viz-head/g) || []).length,
      // The body's own controls must not be in the page at all until opened.
      bodies: (html.match(/Copy prompt/g) || []).length,
      named: html.includes('Business 7'),
    }}));
    """)
    data = __import__("json").loads(out)
    assert data["rows"] == 12, "every prompt should still have a row"
    assert data["bodies"] == 0, "cards are expanded before anyone asked"
    assert data["named"] is True, "a collapsed row must still say who it is"


def test_opening_one_card_does_not_open_the_rest():
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_many(5)})}});
    await loadVisual();
    vizToggle('LEAD-3');
    const html = _el('#visual').innerHTML;
    console.log(JSON.stringify({{bodies: (html.match(/Copy prompt/g) || []).length}}));
    """)
    assert __import__("json").loads(out)["bodies"] == 1


def test_the_list_opens_on_the_only_state_that_needs_him():
    """Three of the four states need nothing from him. Showing all of them
    by default is the same wall of cards in a different order."""
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_many(3)})}});
    await loadVisual();
    console.log(JSON.stringify({{filter: vizFilter, html: _el('#visual').innerHTML}}));
    """)
    data = __import__("json").loads(out)
    assert data["filter"] == "needs_image"
    assert "Need a picture (3)" in data["html"]
    assert "Ready to send (0)" in data["html"]


def test_removed_ones_are_not_in_the_list_at_all():
    """"Remove" has to mean gone from the list, or the button has not done
    what it says. Reversible, so nothing is actually destroyed."""
    import json as _json

    q = _json.loads(_many(2))
    q["queue"][0]["state"] = "skipped"
    q["counts"] = {"needs_image": 1, "needs_check": 0, "ready": 0, "skipped": 1}
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_json.dumps(q)})}});
    await loadVisual();
    const all = (() => {{ vizSetFilter('all'); return _el('#visual').innerHTML; }})();
    const removed = (() => {{ vizSetFilter('skipped'); return _el('#visual').innerHTML; }})();
    console.log(JSON.stringify({{
      inAll: all.includes('Business 0'),
      inRemoved: removed.includes('Business 0'),
    }}));
    """)
    data = __import__("json").loads(out)
    assert data["inAll"] is False, "a removed one is still cluttering the list"
    assert data["inRemoved"] is True, "a removed one cannot be found again"


def test_an_open_card_offers_remove():
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_many(2)})}});
    await loadVisual();
    vizToggle('LEAD-0');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    html = __import__("json").loads(out)["html"]
    assert ">Remove<" in html
    assert "skipViz('LEAD-0', 'Skip')" in html


# ------------------------------------------------------- picking who to run


def _candidates():
    import json as _json

    return _json.dumps({
        "enabled": True,
        "total": 63,
        "groups": [
            {"key": "whatsapp", "label": "On WhatsApp", "count": 41,
             "contactable": True, "capped": True,
             "leads": [{"lead_id": "W1", "name": "Salon One", "category": "Nail salon"},
                       {"lead_id": "W2", "name": "Salon Two", "category": "Hair salon"}]},
            {"key": "email", "label": "Email only", "count": 7,
             "contactable": True, "capped": False,
             "leads": [{"lead_id": "E1", "name": "Clinic One", "category": "Clinic"}]},
            {"key": "unchecked", "label": "Not checked yet", "count": 15,
             "contactable": False, "capped": False, "leads": []},
            {"key": "none", "label": "No way to contact them", "count": 0,
             "contactable": False, "capped": False, "leads": []},
        ],
    })


def test_the_picker_splits_them_by_whether_they_have_whatsapp():
    """The split Lyon asked to work from: it is what decides how a lead gets
    contacted, and so which batch is worth an evening in the image tool."""
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_candidates()})}});
    await loadVisualCandidates();
    console.log(JSON.stringify({{html: _el('#visual-groups').innerHTML}}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "On WhatsApp" in html and "41" in html
    assert "Email only" in html and "7" in html
    assert "Not checked yet" in html and "15" in html
    # A group with nobody in it is not a choice.
    assert "No way to contact them" not in html


def test_a_group_nothing_can_be_written_for_offers_no_button():
    """The brief holds an unchecked lead at the same gate the site brief
    does, so a button there would promise work that cannot happen."""
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_candidates()})}});
    await loadVisualCandidates();
    console.log(JSON.stringify({{html: _el('#visual-groups').innerHTML}}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "runVisualFor('whatsapp'" in html
    assert "runVisualFor('email'" in html
    assert "runVisualFor('unchecked'" not in html
    assert "nothing can be written for these yet" in html


def test_running_for_a_group_sends_that_group_and_the_number_asked_for():
    out = _run_in_node(_VIZ_DOM + f"""
    let sent = null;
    globalThis.fetch = async (url, opts) => {{
      if (url === '/api/visual/run') sent = JSON.parse(opts.body);
      return {{json: async () => ({_candidates()})}};
    }};
    await loadVisualCandidates();
    _el('#viz-limit').value = '10';
    await runVisualFor('whatsapp', null);
    console.log(JSON.stringify({{sent}}));
    """)
    sent = __import__("json").loads(out)["sent"]
    assert sent["groups"] == ["whatsapp"]
    assert sent["limit"] == "10"
    assert sent["lead_ids"] == []


def test_ticking_individual_businesses_sends_exactly_those():
    """"Which ones we are able to use" -- a group is the fast path, a tick is
    the precise one."""
    out = _run_in_node(_VIZ_DOM + f"""
    let sent = null;
    globalThis.fetch = async (url, opts) => {{
      if (url === '/api/visual/run') sent = JSON.parse(opts.body);
      return {{json: async () => ({_candidates()})}};
    }};
    await loadVisualCandidates();
    vizPick('W1', true);
    vizPick('E1', true);
    vizPick('W2', true);
    vizPick('W2', false);
    await runVisualFor('', null);
    console.log(JSON.stringify({{sent, note: _el('#viz-picked-note').textContent}}));
    """)
    data = __import__("json").loads(out)
    assert sorted(data["sent"]["lead_ids"]) == ["E1", "W1"]
    assert data["sent"]["groups"] == []


def test_the_tick_count_is_shown_so_nobody_runs_a_batch_they_did_not_mean():
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_candidates()})}});
    await loadVisualCandidates();
    const before = _el('#viz-picked-note').textContent;
    vizPick('W1', true);
    vizPick('E1', true);
    console.log(JSON.stringify({{before, after: _el('#viz-picked-note').textContent}}));
    """)
    data = __import__("json").loads(out)
    assert data["before"] == "Nothing ticked yet"
    assert data["after"] == "2 ticked"


# ------------------------------------------ the message, and where it sends
#
# Asked after a real run, looking at a checked card: "are these going to be
# the messages that are going to be sent out?" -- about the proofreading box.
# A fair reading of "these exact words must be on the picture", and the kind
# of mistake you only make once a graphic has reached a real business.


def _checked(**over):
    import json as _json

    q = _json.loads(_viz_queue_response())
    row = q["queue"][0]
    row["state"] = "ready"
    # The ordinary case: confirmed, drafted, and step 4 is offering it. The
    # cards that override this are the ones step 4 is holding.
    row["in_send_queue"] = True
    row["send_hold"] = ""
    row.update(over)
    q["counts"] = {"needs_image": 0, "needs_check": 0, "ready": 1,
                   "sent": 0, "skipped": 0}
    return _json.dumps(q)


def _open_ready(payload):
    import re as _re

    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({payload})}});
    await loadVisual();
    vizSetFilter('ready');
    vizToggle('LEAD-1');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    return _re.sub(r"\s+", " ", __import__("json").loads(out)["html"])


def test_the_proofreading_box_says_it_is_not_the_message():
    """It was read as the message. The words are there to be compared against
    the picture and are never sent anywhere."""
    html = _open_ready(_checked(message="Hi, Lyon here. Noticed something."))
    assert "Proofread the picture" in html
    assert "is <b>not</b> the message" in html or "not</b> the message" in html
    assert "never sent anywhere" in html


def test_a_checked_card_shows_the_message_that_will_actually_go():
    """The answer to "is it custom?" belongs on the screen the question is
    asked from."""
    html = _open_ready(_checked(message="Hi, Lyon here. Saw your reviews."))
    assert "Hi, Lyon here. Saw your reviews." in html
    assert "The message this picture goes out with" in html


def test_it_says_where_it_sends_from_because_there_is_no_button_here():
    """A card that says "ready to send" and offers no onward signpost is a
    dead end -- the same shape as a stage with no button."""
    html = _open_ready(_checked(message="Hi there."))
    assert "no separate send button here" in html
    assert "#send-step" in html


def test_a_card_with_no_message_yet_says_what_to_press():
    """It used to assert "Draft messages", which was the instruction this
    card gave instead of a button -- see the test below for why that is now
    a press. The rest of the sentence still has to be there: a message
    written from that business's own research, like the website ones."""
    html = _open_ready(_checked(message=""))
    assert "has not been written yet" in html
    assert "Write its message now" in html
    assert "its own research" in html


def test_an_already_messaged_lead_shows_the_old_message_and_offers_a_new_one():
    """A row on the Outreach Log is what stops a business being written to
    twice, so the draft stage will never touch this lead again -- and its
    picture could never get a message. The button is the one way through."""
    html = _open_ready(_checked(message="Old website pitch.",
                                sent_at="2026-11-02T09:00:00Z"))
    assert "Already messaged on" in html
    assert "2026-11-02T09:00:00Z" in html
    assert "Old website pitch." in html
    assert "Write a new message for this picture" in html
    assert "redraftViz(" in html
    # It is not simply re-offered for sending: the old words are not what
    # this picture is about.
    assert "no separate send button here" not in html


def test_redrafting_puts_it_back_in_the_queue_and_rewrites_the_message():
    out = _run_in_node(_VIZ_DOM + """
    const asked = [];
    globalThis.fetch = async (url, opts) => {
      asked.push(url);
      return {json: async () => ({ok: true, enabled: true, queue: [], counts: {}})};
    };
    await redraftViz('LEAD-1');
    console.log(JSON.stringify({
      posted: asked.filter(u => u === '/api/visual/redraft').length,
      reloaded: asked.filter(u => u === '/api/visual/queue').length,
    }));
    """)
    data = __import__("json").loads(out)
    assert data["posted"] == 1
    assert data["reloaded"] == 1, "the card was left showing the old message"


def test_a_failed_redraft_is_shown_on_the_card():
    out = _run_in_node(_VIZ_DOM + """
    globalThis.fetch = async () => ({json: async () => ({
      error: 'Check the picture first.'
    })});
    await redraftViz('LEAD-1');
    console.log(JSON.stringify({note: _el('#vizr-LEAD-1').textContent}));
    """)
    assert "Check the picture first." in __import__("json").loads(out)["note"]


def test_an_unchecked_card_does_not_show_the_message_yet():
    """Nothing is attached until the words are confirmed, so promising a
    message beside an unchecked picture would describe work not yet done."""
    import json as _json

    q = _json.loads(_viz_queue_response())
    q["queue"][0].update(state="needs_check", message="Hi, Lyon here.")
    q["counts"] = {"needs_image": 0, "needs_check": 1, "ready": 0, "skipped": 0}
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_json.dumps(q)})}});
    await loadVisual();
    vizSetFilter('needs_check');
    vizToggle('LEAD-1');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "Proofread the picture" in html
    assert "The message this picture goes out with" not in html


def test_the_send_queue_names_which_picture_it_is_carrying():
    """The queue has always carried `shot_kind`. The page said "site
    screenshot" whatever arrived, so an infographic -- which IS the pitch on
    that track -- was described as something else entirely."""
    out = _run_in_node(_VIZ_DOM + """
    const base = {lead_id: 'a', name: 'Salon', phone: '+60123',
                  phone_e164: '+60123', message: 'hi', has_shot: true,
                  wa_link: 'https://wa.me/60123'};
    globalThis.fetch = async () => ({json: async () => ({
      queue: [{...base, shot_kind: 'visual'},
              {...base, lead_id: 'b', name: 'Shop', shot_kind: 'site'}],
      excluded: [],
    })});
    await loadWhatsAppInline();
    console.log(JSON.stringify({html: _el('#wa-inline').innerHTML}));
    """)
    html = __import__("json").loads(out)["html"]
    assert "Infographic attached" in html
    assert "Site screenshot attached" in html


def test_the_send_queue_warns_when_a_message_is_the_old_website_pitch():
    """Changing the track changes what is drafted next; it cannot reach back
    into messages already on the Sheet, and those are what step 4 sends. So a
    campaign switched to graphics still had a queue full of "Website:
    https://..." with a picture attached, and the setting looked inert."""
    out = _run_in_node(_VIZ_DOM + """
    const base = {lead_id: 'a', name: 'Salon', phone: '+60123',
                  phone_e164: '+60123', has_shot: true, shot_kind: 'visual',
                  wa_link: 'https://wa.me/60123'};
    globalThis.fetch = async () => ({json: async () => ({
      queue: [{...base, message: 'Website: https://x.y', stale_pitch: true},
              {...base, lead_id: 'b', name: 'Shop', message: 'About your picture.'}],
      excluded: [],
    })});
    await loadWhatsAppInline();
    console.log(JSON.stringify({html: _el('#wa-inline').innerHTML}));
    """)
    html = " ".join(__import__("json").loads(out)["html"].split())
    assert "written for the website pitch" in html
    # Exactly once: the flagged card, not the one beside it.
    assert html.count("written for the website pitch") == 1


# ---------------- a message written for the pitch the campaign no longer makes


def _queue_row(**over):
    """One drafted, unsent WhatsApp lead."""
    row = {
        "Lead ID": "a",
        "Business Name": "NS Aesthetics",
        "Channel": "WhatsApp",
        "Status": "Drafted",
        "WhatsApp Message": "We built you a site. Website: https://ns.ai.studio",
        "Drafted At": "2026-09-01T00:00:00Z",
    }
    row.update(over)
    return {"a": (2, row)}


_QUEUE_LEADS = {
    "a": (2, {"Lead ID": "a", "Business Name": "NS Aesthetics",
              "Phone": "+60123456789", "WhatsApp Reachable": "yes"})
}
_QUEUE_BRIEFS = {"a": (2, {"Lead ID": "a", "Preview URL": "https://ns.ai.studio"})}


def _tracked_client(monkeypatch, sheets, track):
    monkeypatch.setattr(dash, "_sheets_client", lambda config: sheets)
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    # Pinned, not inherited: the template's own track would make this
    # assertion mean whatever that file says next.
    config = replace(
        config,
        secrets=replace(config.secrets, sheet_id="s"),
        outreach=replace(config.outreach, track=track),
    )
    return create_app(config).test_client()


def test_a_queued_message_still_selling_the_page_is_flagged(monkeypatch):
    """Switching the track changes what is drafted next. It cannot reach back
    into messages already on the Sheet -- and those are what step 4 sends, so
    a campaign moved to graphics still queued "Website: https://..." with a
    picture attached, and the setting looked like it had done nothing."""
    sheets = _GuardedSheets(
        outreach=_queue_row(), leads=_QUEUE_LEADS, briefs=_QUEUE_BRIEFS
    )
    client = _tracked_client(monkeypatch, sheets, "visual")

    body = client.get("/api/whatsapp/queue").get_json()

    assert [m["stale_pitch"] for m in body["queue"]] == [True]


def test_a_message_rewritten_for_the_picture_is_not_flagged(monkeypatch):
    """The control. A flag that fires on everything is one nobody reads."""
    sheets = _GuardedSheets(
        outreach=_queue_row(**{
            "WhatsApp Message": "Noticed people finding you and not calling.",
        }),
        leads=_QUEUE_LEADS,
        briefs=_QUEUE_BRIEFS,
    )
    client = _tracked_client(monkeypatch, sheets, "visual")

    assert client.get("/api/whatsapp/queue").get_json()["queue"][0][
        "stale_pitch"
    ] is False


def test_the_both_track_is_never_flagged_for_linking_the_page(monkeypatch):
    """`both` promises the picture *and* the link. Flagging it would be
    telling someone to undo the thing the setting does."""
    sheets = _GuardedSheets(
        outreach=_queue_row(), leads=_QUEUE_LEADS, briefs=_QUEUE_BRIEFS
    )
    client = _tracked_client(monkeypatch, sheets, "both")

    assert client.get("/api/whatsapp/queue").get_json()["queue"][0][
        "stale_pitch"
    ] is False


def test_the_website_track_pays_no_read_for_a_flag_it_cannot_raise(monkeypatch):
    """A campaign pitching a page must not buy a Sheet round trip to be told
    its message mentions the page. Same rule as the Visual Briefs read."""
    reads = []

    class _Counting(_GuardedSheets):
        def read_rows(self, tab):
            reads.append(tab.title)
            return super().read_rows(tab)

    sheets = _Counting(
        outreach=_queue_row(), leads=_QUEUE_LEADS, briefs=_QUEUE_BRIEFS
    )
    client = _tracked_client(monkeypatch, sheets, "website")

    body = client.get("/api/whatsapp/queue").get_json()

    assert schema.SITE_BRIEFS.title not in reads
    assert body["queue"][0]["stale_pitch"] is False


def test_a_lead_with_no_page_built_is_never_flagged(monkeypatch):
    """Nothing to have been written about. Matching on a blank Preview URL
    would flag every message on the track."""
    sheets = _GuardedSheets(
        outreach=_queue_row(), leads=_QUEUE_LEADS,
        briefs={"a": (2, {"Lead ID": "a", "Preview URL": ""})},
    )
    client = _tracked_client(monkeypatch, sheets, "visual")

    assert client.get("/api/whatsapp/queue").get_json()["queue"][0][
        "stale_pitch"
    ] is False


# ------------------------------------- asking the key which models it has


def _model_client(monkeypatch):
    monkeypatch.setattr(dash, "_sheets_client", lambda config: _GuardedSheets())
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, sheet_id="s"))
    return create_app(config).test_client()


def test_the_catalogue_endpoint_answers_with_the_names(monkeypatch):
    """A terminal command the owner cannot run is not an answer, and
    `list-models` only ever spoke to Gemini anyway."""
    monkeypatch.setattr(
        "outreach.providers.llm.available_models", lambda c: ["a", "b"]
    )
    client = _model_client(monkeypatch)

    body = client.get("/api/model/catalogue").get_json()

    assert body["models"] == ["a", "b"]


def test_the_catalogue_endpoint_reports_a_refusal_rather_than_500(monkeypatch):
    """The page can only render a 500 as "could not reach the dashboard",
    which is a different problem entirely."""
    from outreach.providers.llm import LLMError

    def boom(_c):
        raise LLMError("that key was rejected")

    monkeypatch.setattr("outreach.providers.llm.available_models", boom)
    client = _model_client(monkeypatch)

    res = client.get("/api/model/catalogue")

    assert res.status_code == 200
    assert "rejected" in res.get_json()["error"]


def test_the_model_panel_can_ask_and_fills_the_box_when_clicked():
    """Retyping a name read off a screen is how a typo becomes a 404 that
    looks exactly like a retired model.

    The stub DOM has no real buttons, so the handlers are lifted out of the
    HTML the page built and run -- which is what proves they name the right
    function and the right field, rather than that a string was concatenated.
    """
    out = _run_in_node(_VIZ_DOM + """
    globalThis.fetch = async (u) => ({json: async () => (
      String(u).includes('catalogue')
        ? {provider: 'ollama-cloud', models: ['alpha:cloud', 'beta:cloud']}
        : {provider: 'ollama-cloud', model: 'old:cloud', outreach_model: '',
           providers: ['ollama-cloud'], key_env: 'OLLAMA_API_KEY',
           key_needed: true, key_set: true, base_url: '', guaranteed: false}
    )});
    await loadModel();
    drawModel(true);
    await loadCatalogue();
    const html = _el('#m-catalogue').innerHTML;
    const handlers = [...html.matchAll(/onclick="([^"]+)"/g)].map(m => m[1]);
    // The stub does not parse innerHTML, so the input's starting value is
    // not readable here -- what matters is that these handlers write to it.
    eval(handlers[0]);
    eval(handlers[1]);
    console.log(JSON.stringify({
      html, note: _el('#m-cat-msg').textContent, handlers: handlers.length,
      model: _el('#m-model').value, message: _el('#m-omodel').value,
    }));
    """)
    d = __import__("json").loads(out)
    assert "alpha:cloud" in d["html"] and "beta:cloud" in d["html"]
    assert "2 from ollama-cloud" in d["note"]
    assert d["handlers"] == 4, "two names, two buttons each"
    # The first row's two buttons: the model, and the message model.
    assert d["model"] == "alpha:cloud"
    assert d["message"] == "alpha:cloud"


def test_the_model_panel_shows_why_the_catalogue_failed():
    """"Nothing happened" is the one report a person can make otherwise."""
    out = _run_in_node(_VIZ_DOM + """
    globalThis.fetch = async (u) => ({json: async () => (
      String(u).includes('catalogue')
        ? {error: 'OLLAMA_API_KEY is not set in .env.'}
        : {provider: 'ollama-cloud', model: 'm', outreach_model: '',
           providers: ['ollama-cloud'], key_env: 'OLLAMA_API_KEY',
           key_needed: false, key_set: false, base_url: '', guaranteed: false}
    )});
    await loadModel();
    drawModel(true);
    await loadCatalogue();
    console.log(JSON.stringify({html: _el('#m-catalogue').innerHTML}));
    """)
    assert "OLLAMA_API_KEY is not set" in __import__("json").loads(out)["html"]


def test_the_model_panel_prints_no_example_model_name():
    """Four were hardcoded as placeholders. Each is a name with a shelf life
    sitting on the one screen somebody copies from."""
    page = dash.PAGE if hasattr(dash, "PAGE") else ""
    if not page:
        import outreach.dashboard as _d
        page = _d._page() if hasattr(_d, "_page") else ""
    assert "kimi-k2" not in page
    assert "gemini-2.5-flash" not in page


def test_the_message_model_says_it_writes_both_kinds_of_message():
    """Asked outright: does the writing model cover the website message and
    the picture message? It does -- there is no third model."""
    out = _run_in_node(_VIZ_DOM + """
    globalThis.fetch = async () => ({json: async () => (
      {provider: 'anthropic', model: 'm', outreach_model: '',
       providers: ['anthropic'], key_env: 'ANTHROPIC_API_KEY',
       key_needed: true, key_set: true, base_url: '', guaranteed: true})});
    await loadModel();
    drawModel(true);
    console.log(JSON.stringify({html: _el('#model').innerHTML}));
    """)
    html = " ".join(__import__("json").loads(out)["html"].split())
    assert "the website ones and the infographic ones alike" in html


def test_the_infographic_card_says_whether_step_four_has_it():
    """"It isn't brought over" was the only report a person could make about
    a card saying "ready to send" beside a queue that never offered it."""
    import json as _json

    q = _json.loads(_viz_queue_response())
    q["queue"][0].update(
        state="ready", message="About your picture.",
        in_send_queue=False, send_hold="it was drafted as an email, so it is "
                                       "in Gmail drafts",
    )
    q["counts"] = {"needs_image": 0, "needs_check": 0, "ready": 1,
                   "sent": 0, "skipped": 0}
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_json.dumps(q)})}});
    await loadVisual();
    vizSetFilter('ready');
    vizToggle('LEAD-1');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    html = " ".join(__import__("json").loads(out)["html"].split())
    assert "Not in step 4 yet" in html
    assert "in Gmail drafts" in html
    assert "Waiting in" not in html


def test_the_infographic_card_confirms_when_step_four_does_have_it():
    """The control: the reassuring half has to render too, or every card
    reads as a problem."""
    import json as _json

    q = _json.loads(_viz_queue_response())
    q["queue"][0].update(state="ready", message="About your picture.",
                         in_send_queue=True, send_hold="")
    q["counts"] = {"needs_image": 0, "needs_check": 0, "ready": 1,
                   "sent": 0, "skipped": 0}
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_json.dumps(q)})}});
    await loadVisual();
    vizSetFilter('ready');
    vizToggle('LEAD-1');
    console.log(JSON.stringify({{html: _el('#visual').innerHTML}}));
    """)
    html = " ".join(__import__("json").loads(out)["html"].split())
    assert "Waiting in" in html
    assert "Not in step 4 yet" not in html


def test_a_sent_graphic_is_not_in_the_working_lists():
    """Sent is finished work. Leaving it under Ready made that list grow by
    one every time a message went out."""
    import json as _json

    q = _json.loads(_viz_queue_response())
    q["queue"][0].update(state="sent", message="About your picture.",
                         sent_at="2026-09-02T00:00:00Z")
    q["counts"] = {"needs_image": 0, "needs_check": 0, "ready": 0,
                   "sent": 1, "skipped": 0}
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async () => ({{json: async () => ({_json.dumps(q)})}});
    await loadVisual();
    const tabs = _el('#visual').innerHTML;
    vizSetFilter('all');
    const inAll = _el('#visual').innerHTML;
    vizSetFilter('sent');
    console.log(JSON.stringify({{
      tabs, inAll, inSent: _el('#visual').innerHTML,
    }}));
    """)
    d = __import__("json").loads(out)
    assert "Sent (1)" in d["tabs"]
    assert "Nothing in this list" in d["inAll"]
    assert "Your Nail Crush" in d["inSent"]


def test_a_held_card_offers_the_repair_where_the_problem_is_read():
    """"It still doesn't show any button" -- the repair existed, in step 4's
    excluded list, which is a different screen from the one saying the
    message is ready."""
    html = _open_ready(_checked(
        message="About your picture.",
        in_send_queue=False, can_unblock=True,
        send_hold="its Outreach Log row does not say Drafted",
    ))
    assert "Put it in step 4" in html
    assert "unblockViz(" in html


def test_a_hold_no_button_can_fix_offers_none():
    """A button beside "it has no phone number" would press and change
    nothing, which reads as the dashboard being broken."""
    html = _open_ready(_checked(
        message="About your picture.",
        in_send_queue=False, can_unblock=False,
        send_hold="there is no phone number on its Leads row",
    ))
    assert "Put it in step 4" not in html
    assert "no phone number" in html


def test_every_drafted_card_can_be_rewritten():
    """Asked for by name. It was only ever offered on an already-sent lead,
    where the old words are about a different pitch -- but a message you
    simply do not like is the same problem with no button."""
    html = _open_ready(_checked(message="About your picture."))
    assert "Rewrite this message" in html
    assert "redraftViz(" in html


def test_pressing_the_repair_redraws_both_panels():
    """The card stops saying it is held and step 4 grows a row. Redrawing one
    and not the other is how a page ends up disagreeing with itself."""
    payload = _checked(message="About your picture.", in_send_queue=False,
                       can_unblock=True, send_hold="it is not Drafted")
    out = _run_in_node(_VIZ_DOM + f"""
    // The page defines its own loadWhatsAppInline, which shadows anything
    // put on globalThis -- so the refresh is observed by the request it
    // makes rather than by a stub it never sees.
    const calls = [];
    globalThis.fetch = async (u, opts) => {{
      calls.push(String(u));
      return {{json: async () => (String(u).includes('undo-send')
        ? {{ok: true, cleared: ['Status (Skipped -> Drafted)']}}
        : {payload})}};
    }};
    await loadVisual();
    vizSetFilter('ready');
    vizToggle('LEAD-1');
    await unblockViz('LEAD-1');
    console.log(JSON.stringify({{calls}}));
    """)
    calls = __import__("json").loads(out)["calls"]
    assert any("undo-send" in c for c in calls)
    after = calls[calls.index(next(c for c in calls if "undo-send" in c)) + 1:]
    assert any("/api/visual/queue" in c for c in after), "the card was not redrawn"
    assert any("/api/whatsapp/queue" in c for c in after), "step 4 was not refreshed"


def test_a_card_with_no_message_offers_a_button_not_an_instruction():
    """"Press Draft messages in step 4" is prose where a press belongs, and
    it was the whole of what this card offered to a graphic confirmed before
    confirming started writing the message. Reported as "still not showing
    any send to queue button. Are you sure you fixed it?\""""
    html = _open_ready(_checked(message=""))

    assert "Write its message now" in html
    assert "writeVizMessage(" in html
    # The old wording told the reader to go somewhere else and do it by hand.
    assert "Press <b>Draft messages</b>" not in html


def test_writing_the_message_redraws_both_panels():
    """This card gains the message and step 4 gains the row. Redrawing one
    and not the other is how a page ends up disagreeing with itself."""
    payload = _checked(message="")
    out = _run_in_node(_VIZ_DOM + f"""
    const calls = [];
    globalThis.fetch = async (u) => {{
      calls.push(String(u));
      return {{json: async () => (String(u).includes('write-message')
        ? {{ok: true, message_written: true}}
        : {payload})}};
    }};
    await loadVisual();
    vizSetFilter('ready');
    vizToggle('LEAD-1');
    await writeVizMessage('LEAD-1');
    console.log(JSON.stringify({{calls}}));
    """)
    calls = __import__("json").loads(out)["calls"]
    assert any("write-message" in c for c in calls)
    after = calls[calls.index(next(c for c in calls if "write-message" in c)) + 1:]
    assert any("/api/visual/queue" in c for c in after), "the card was not redrawn"
    assert any("/api/whatsapp/queue" in c for c in after), "step 4 was not refreshed"


def test_a_card_that_already_has_one_says_so_rather_than_claiming_a_write():
    """Saying "written" would be a lie; saying nothing reads as a dead
    button."""
    payload = _checked(message="")
    out = _run_in_node(_VIZ_DOM + f"""
    globalThis.fetch = async (u) => ({{json: async () => (
      String(u).includes('write-message')
        ? {{ok: true, unchanged: true}}
        : {payload})}});
    await loadVisual();
    vizSetFilter('ready');
    vizToggle('LEAD-1');
    await writeVizMessage('LEAD-1');
    console.log(JSON.stringify({{note: _el('#vizwn-LEAD-1').textContent}}));
    """)
    note = __import__("json").loads(out)["note"]
    assert "already has a message" in note
