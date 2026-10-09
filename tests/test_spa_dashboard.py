"""The React page, and the endpoints written for it.

A green suite has already once sat beside a dashboard that would not render,
so these tests avoid the shape that allowed it. They do not assert that a
route exists: they serve the built page, follow the asset URLs it actually
names, and read the JSON body the front end will parse. The one that matters
most is `test_the_built_page_asks_for_assets_the_app_can_serve` -- the base
path is the whole difference between a working page and a blank white screen
with two 404s that never reach the Python log.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from outreach import dashboard as dash
from outreach.config import load_config
from outreach.dashboard import create_app

WEB = Path(dash.__file__).parent / "web"


@pytest.fixture
def client():
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    return create_app(config).test_client()


def configured(**secrets):
    """A config with the keys filled in, so summary returns counts."""
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    return replace(config, secrets=replace(config.secrets, **secrets))


# ------------------------------------------------------------- the page


def test_the_built_page_is_committed():
    """Lyon has no Node and must never need it.

    The build runs here and its output is committed, so a checkout on a
    Windows machine with Python alone can serve the page. If this fails the
    answer is `npm run build` in `frontend/`, not a build step in the batch
    file.
    """
    assert (WEB / "index.html").exists(), "run `npm run build` in frontend/"


def test_the_old_page_links_to_the_new_one(client):
    """A page reachable only by typing an address is one nobody opens.

    The reachability stage shipped once with no button and never ran; this
    is the same shape of mistake with a front end instead of a stage.
    """
    assert b'href="/app"' in client.get("/").data


def test_the_page_is_served_at_app(client):
    response = client.get("/app")
    assert response.status_code == 200
    assert b'<div id="root">' in response.data


def test_the_built_page_asks_for_assets_the_app_can_serve(client):
    """Follow every URL the page names and require a 200.

    Vite writes absolute asset paths, and the default base is `/`, which the
    dashboard does not serve -- the page would load, render nothing, and log
    nothing on the Python side. Asserting the file exists on disk would not
    have caught that; only asking for the URL in the HTML does.
    """
    html = client.get("/app").get_data(as_text=True)
    urls = re.findall(r'(?:src|href)="(/[^"]+)"', html)
    assert urls, "the built page referenced no assets at all"
    for url in urls:
        assert client.get(url).status_code == 200, f"{url} did not load"


def test_an_unknown_app_path_gets_the_page_not_a_404(client):
    """The page owns its own routing below /app."""
    assert client.get("/app/leads").status_code == 200


def test_a_missing_asset_is_still_a_404(client):
    assert client.get("/app/assets/nope.js").status_code == 404


def test_the_asset_route_cannot_escape_the_build_directory(client):
    """The one path that takes a filename off the wire.

    `.env` sits two levels above the build directory, and a route that
    joined paths itself would happily read it.
    """
    for attempt in ("../../../.env", "..%2f..%2f..%2f.env", "../../config", "../config.py"):
        response = client.get(f"/app/{attempt}")
        # Either refused outright, or normalised back to the page itself.
        # What must never happen is a file from outside the build directory
        # coming back with a 200, so the body is checked and not just the code.
        if response.status_code == 200:
            body = response.get_data(as_text=True)
            assert '<div id="root">' in body, f"/app/{attempt} served something else"
        else:
            assert response.status_code in (301, 308, 400, 404)


def test_a_favicon_is_served_so_the_console_stays_quiet(client):
    """Browsers ask for /favicon.ico unprompted, on every page.

    Left unanswered it logged a 404 on every visit -- noise of exactly the
    shape a genuinely broken asset makes, which is how a real one hides.
    """
    response = client.get("/favicon.ico")
    assert response.status_code in (200, 301, 302, 308)


def test_each_page_has_its_own_home_screen_manifest(client):
    old = client.get("/manifest.webmanifest").get_json()
    new = client.get("/app/manifest.webmanifest").get_json()
    assert old["start_url"] == "/"
    assert new["start_url"] == "/app/"
    assert new["display"] == "standalone"
    # /app is the address people type, and a scope of "/app/" would leave it
    # outside the installed app -- so the icon would open a browser tab.
    assert new["scope"] == "/app"


def test_the_phone_instructions_name_the_new_page(capsys, monkeypatch):
    """The address printed here is the one that gets typed into a phone.

    It named the old dashboard, so the page meant to be used on a phone had
    to be found by hand afterwards.
    """
    import outreach.dashboard as dash

    monkeypatch.setattr(dash, "lan_addresses", lambda: ["192.168.1.50"])

    class Stop(Exception):
        pass

    class FakeApp:
        def run(self, **kwargs):
            raise Stop

    monkeypatch.setattr(dash, "create_app", lambda config: FakeApp())
    monkeypatch.setattr(
        dash, "load_config",
        lambda *a, **k: configured(dashboard_password="x", sheet_id=""),
    )

    with pytest.raises(Stop):
        dash.serve(port=8765, open_browser=False, phone=True)

    printed = capsys.readouterr().out
    assert "http://192.168.1.50:8765/app" in printed
    assert "Add to Home Screen" in printed
    assert "https" in printed, "the startup banner does not warn about the scheme"


# --------------------------------------------------- endpoints for the page


def test_leads_returns_the_sheet_as_rows(monkeypatch):
    class FakeSheets:
        def read_rows(self, tab):
            return {
                "L1": (2, {
                    "Lead ID": "L1",
                    "Stage": "Discovered",
                    "Business Name": "Kedai Kopi",
                    "Rating": "4.6",
                    "Review Count": "231",
                    "Website Status": "social_only",
                    "Phone": "+60123456789",
                    "Owner Notes": "called once",
                }),
            }

    monkeypatch.setattr(dash, "_sheets_client", lambda config: FakeSheets())
    client = create_app(configured(sheet_id="s")).test_client()

    leads = client.get("/api/leads").get_json()["leads"]

    assert len(leads) == 1
    row = leads[0]
    assert row["place_id"] == "L1"
    assert row["business_name"] == "Kedai Kopi"
    # Numbers arrive as strings from the Sheet and the table sorts on them.
    assert row["rating"] == 4.6
    assert row["review_count"] == 231
    assert row["owner_notes"] == "called once"
    # Never absent: the page reads it on every row.
    assert row["whatsapp_reachable"] == "unknown"


def test_leads_is_empty_rather_than_broken_before_setup():
    client = create_app(configured(sheet_id="")).test_client()
    assert client.get("/api/leads").get_json() == {"leads": []}


def test_a_note_is_written_as_a_user_edit(monkeypatch):
    """Owner Notes is a manual column, so this must pass `user_edit`.

    Without it the write is refused by `guard_manual_column` and the note is
    silently lost -- which is exactly the bug the guard was added to stop,
    approached from the other side.
    """
    seen = {}

    class FakeSheets:
        def update_column(self, tab, column, values, *, user_edit=False):
            seen.update(tab=tab.title, column=column, values=values, user_edit=user_edit)
            return 1

    monkeypatch.setattr(dash, "_sheets_client", lambda config: FakeSheets())
    client = create_app(configured(sheet_id="s")).test_client()

    body = client.post("/api/lead-note", json={"lead_id": "L1", "note": "hi"}).get_json()

    assert body == {"ok": True}
    assert seen["column"] == "Owner Notes"
    assert seen["user_edit"] is True
    assert seen["values"] == {"L1": "hi"}


def test_a_note_for_a_row_that_is_not_there_says_so(monkeypatch):
    class FakeSheets:
        def update_column(self, tab, column, values, *, user_edit=False):
            return 0

    monkeypatch.setattr(dash, "_sheets_client", lambda config: FakeSheets())
    client = create_app(configured(sheet_id="s")).test_client()

    body = client.post("/api/lead-note", json={"lead_id": "nope", "note": "x"}).get_json()
    assert "nope" in body["error"]


def test_the_model_endpoint_reports_the_places_key_without_revealing_it():
    """Presence only. The value never reaches the page, a log or a report."""
    client = create_app(configured(places_api_key="AIzaSyNOTREAL")).test_client()

    body = client.get("/api/model").get_json()

    assert body["places_key_set"] is True
    assert "AIzaSyNOTREAL" not in json.dumps(body)


def test_a_places_key_saved_from_the_page_is_written_write_only(monkeypatch, tmp_path):
    """The setup screen saves both keys at once, so this branch is new.

    Everything that touches the real machine is redirected: a run that left
    a key in the developer's own `.env`, or forked the tracked campaign,
    would break every later command on that machine.
    """
    import outreach.campaign_edit as campaign_edit
    import outreach.env_edit as env_edit

    written: dict[str, str] = {}
    monkeypatch.setattr(
        env_edit, "set_key", lambda path, key, value: written.__setitem__(key, value)
    )
    fork = tmp_path / "campaign.local.yaml"
    fork.write_text(Path("config/campaigns/penang-my.yaml").read_text())
    monkeypatch.setattr(campaign_edit, "fork_to_local", lambda path: fork)

    client = create_app(configured()).test_client()
    body = client.post("/api/model", json={"places_key": "AIza-test"}).get_json()

    assert body == {"ok": True}
    assert written["PLACES_API_KEY"] == "AIza-test"
    # The tracked campaign is never written to at runtime.
    assert "AIza-test" not in Path("config/campaigns/penang-my.yaml").read_text()


def test_testing_a_key_never_echoes_it_back():
    """A test button that printed the key back would undo the whole rule."""
    client = create_app(configured()).test_client()

    body = client.post(
        "/api/test-key", json={"type": "places", "key": "AIza-secret-value"}
    ).get_json()

    assert "AIza-secret-value" not in json.dumps(body)


def test_an_unknown_key_type_is_refused():
    client = create_app(configured()).test_client()
    response = client.post("/api/test-key", json={"type": "banana"})
    assert response.status_code == 400


def test_testing_a_places_key_reports_what_google_said(monkeypatch):
    from outreach.providers import places

    class Boom:
        def __init__(self, api_key, **kw):
            pass

        def search_text(self, *a, **kw):
            raise places.PlacesError("Places refused the key (403).")

    monkeypatch.setattr(places, "PlacesClient", Boom)
    client = create_app(configured()).test_client()

    body = client.post("/api/test-key", json={"type": "places", "key": "x"}).get_json()

    assert body["valid"] is False
    assert "403" in body["message"]


def test_the_campaign_endpoint_carries_the_compliance_country(client):
    """Shown on the page, not editable there -- so it has to be sent."""
    body = client.get("/api/campaign").get_json()
    assert body["country_code"]


# ------------------------------------------------- the wiring between them


CLIENT_TS = Path("frontend/src/api/client.ts")


def test_every_endpoint_the_page_calls_is_a_route_the_app_serves():
    """The join between two languages, checked in the one place it can be.

    TypeScript proves the views call methods that exist on the client. It
    cannot prove those methods name a URL Python answers -- a path typed one
    character wrong compiles, builds, ships, and then returns 404 to a page
    that shows an empty queue and calls it "nothing to do". Three
    `render_lovable_prompt` call sites were unwired for exactly this reason
    and every other test still passed.
    """
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    rules = {
        str(rule).replace("<lead_id>", "").replace("<path:asset>", "")
        for rule in create_app(config).url_map.iter_rules()
    }

    called = set(re.findall(r"'(/api/[^'`$]*)", CLIENT_TS.read_text()))
    assert called, "found no endpoints in the client -- has the file moved?"

    missing = sorted(path for path in called if path not in rules)
    assert not missing, f"the page calls routes the dashboard does not serve: {missing}"


def test_the_send_call_passes_the_number_python_worked_out():
    """The rule that a wrong answer to sends a message to a stranger.

    `phone_e164` is normalised once, on the Python side, where the lead's
    own country is known. If the browser stops sending it back the server
    falls through to normalising the display number with the *campaign's*
    country -- which is how a UK lead went out as 60447450225230, accepted
    by WhatsApp, reported as sent, delivered to nobody.
    """
    source = CLIENT_TS.read_text()
    body = source[source.index("async sendWhatsApp"):]
    body = body[: body.index("async ", 1)]
    assert "phone_e164," in body, "the client stopped sending the dialled number"

    # And the queue screen must actually hand it over: an optional argument
    # nobody passes is the same bug with a compiling signature.
    view = Path("frontend/src/views/SendQueueView.tsx").read_text()
    assert "item.e164_phone" in view


def test_the_page_never_builds_a_dialling_code_itself():
    """No country code is ever inferred in the browser.

    A US national number opens with 504, which is Honduras. The only place
    that may turn a number into E.164 is the Python side, once.
    """
    for source in Path("frontend/src").rglob("*.ts*"):
        text = source.read_text()
        assert "'+60'" not in text and '"+60"' not in text, source
        assert "+ '60'" not in text, source


# ------------------------------------------------------- getting to the page


def guarded():
    """A dashboard with a password set, so the login guard is live."""
    return create_app(configured(dashboard_password="hunter2", sheet_id="")).test_client()


def test_signing_in_returns_you_to_the_page_you_asked_for():
    """Losing it meant /app was unreachable for anyone with a password.

    Ask for the new page, get stopped at the login, sign in -- and land on
    the old dashboard, which looks exactly as though the new page was never
    shipped. Reported three times as "it still looks the same".
    """
    client = guarded()

    bounced = client.get("/app")
    assert bounced.status_code == 302
    from urllib.parse import parse_qs, urlparse

    carried = parse_qs(urlparse(bounced.headers["Location"]).query)["next"][0]
    assert carried.startswith("/app")

    signed_in = client.post("/login", data={"password": "hunter2", "next": "/app"})
    assert signed_in.status_code == 302
    assert signed_in.headers["Location"] == "/app"


def test_signing_in_with_no_destination_still_goes_home():
    client = guarded()
    response = client.post("/login", data={"password": "hunter2"})
    assert response.headers["Location"] == "/"


def test_the_login_page_will_not_forward_you_off_this_site():
    """The destination comes from a query string, so a stranger picks it.

    A login page that redirects anywhere is a redirector wearing a trusted
    address. Both of these are read by browsers as another host, despite
    starting with a slash.
    """
    for elsewhere in ("//example.com", "/\\example.com", "https://example.com", "javascript:alert(1)"):
        client = guarded()
        response = client.post("/login", data={"password": "hunter2", "next": elsewhere})
        assert response.headers["Location"] == "/", elsewhere


def test_a_wrong_password_keeps_the_destination():
    """Otherwise a typo quietly costs you the page you were going to."""
    client = guarded()
    response = client.post("/login", data={"password": "wrong", "next": "/app"})
    assert response.status_code == 401
    assert b'value="/app"' in response.data


def test_the_link_to_the_new_page_is_a_button_not_a_footnote(client):
    page = client.get("/").get_data(as_text=True)
    link = page[page.index('href="/app"'):][:400]
    assert "background" in link, "the link to the new dashboard is not styled as a button"


# ------------------------------------------------------- phone access check


def run_phone_check(monkeypatch, *, password=True, lan=("192.168.1.50",),
                    open_ports=(), platform="win32", firewall_rule=True):
    """Drive `outreach phone-check` with the machine's answers faked.

    Every branch is a different real failure that looks the same from a
    phone, so each one is exercised rather than assumed.
    """
    import socket
    import subprocess

    from click.testing import CliRunner

    import outreach.cli as cli_mod
    import outreach.dashboard as dash_mod
    import outreach.env_edit as env_edit_mod

    monkeypatch.setattr(env_edit_mod, "is_set", lambda path, key: password)
    monkeypatch.setattr(dash_mod, "lan_addresses", lambda: list(lan))
    monkeypatch.setattr(cli_mod.sys, "platform", platform)

    def fake_connect(address, timeout=None):
        if address[0] not in open_ports:
            raise OSError("refused")

        class Socket:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Socket()

    monkeypatch.setattr(socket, "create_connection", fake_connect)

    class Completed:
        returncode = 0 if firewall_rule else 1
        stdout = "Rule Name: SME Outreach dashboard" if firewall_rule else "No rules match"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Completed())

    return CliRunner().invoke(cli_mod.main, ["phone-check"]).output


def test_phone_check_offers_every_address_a_phone_might_reach(monkeypatch):
    """One guess was the fault behind "nothing loads".

    The address came from asking the routing table what reaches the
    internet, which on a machine carrying a VPN, WSL, Docker or Hyper-V is a
    virtual adapter no phone can talk to -- and the failure is a page that
    never loads, identical to a blocked firewall. Every address is offered
    now, real network cards first.
    """
    out = run_phone_check(
        monkeypatch,
        lan=("192.168.1.50", "172.28.5.1"),
        open_ports=("127.0.0.1", "172.28.5.1", "192.168.1.50"),
    )
    assert "http://192.168.1.50:8765/app" in out
    assert "http://172.28.5.1:8765/app" in out
    assert "virtual adapter" in out
    assert "try these on your" in out.lower()


def test_lan_addresses_puts_real_network_cards_before_virtual_ones(monkeypatch):
    """The ordering itself, tested on the real function.

    Faking `lan_addresses` to check its own ordering would only test the
    fake. This drives the two sources it reads instead.
    """
    import socket

    import outreach.dashboard as dash

    # What a Windows machine with WSL or Docker Desktop looks like: the
    # route to the internet leaves through a virtual adapter, so the old
    # single guess returned an address no phone can ever reach.
    monkeypatch.setattr(dash, "_routed_address", lambda: "172.28.5.1")
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [
            (None, None, None, "", ("192.168.1.50", 0)),
            (None, None, None, "", ("127.0.0.1", 0)),
            (None, None, None, "", ("169.254.7.7", 0)),
        ],
    )

    found = dash.lan_addresses()

    assert found == ["192.168.1.50", "172.28.5.1"], found
    # Loopback is not somewhere a phone can go, and 169.254 means DHCP
    # never answered -- offering either wastes an attempt.
    assert "127.0.0.1" not in found
    assert "169.254.7.7" not in found


def test_lan_address_still_answers_with_the_single_best_guess(monkeypatch):
    import outreach.dashboard as dash

    monkeypatch.setattr(dash, "lan_addresses", lambda: ["192.168.1.50", "172.28.5.1"])
    assert dash.lan_address() == "192.168.1.50"

    monkeypatch.setattr(dash, "lan_addresses", lambda: [])
    assert dash.lan_address() == ""


def test_phone_check_names_a_missing_password_and_the_line_that_fixes_it(monkeypatch):
    out = run_phone_check(monkeypatch, password=False)
    assert "No dashboard password" in out
    assert "DASHBOARD_PASSWORD=" in out


def test_phone_check_never_prints_the_password(monkeypatch):
    """Secrets are write-only, and a diagnostic is still a report."""
    out = run_phone_check(monkeypatch, password=True, open_ports=("127.0.0.1", "192.168.1.50"))
    assert "hunter2" not in out
    # `is_set` reports presence; nothing here has access to the value at all.
    assert "DASHBOARD_PASSWORD=" not in out.split("A dashboard password is set")[1]


def test_phone_check_says_when_the_dashboard_is_not_running(monkeypatch):
    out = run_phone_check(monkeypatch, open_ports=())
    assert "Start it first" in out
    assert "Dashboard.bat" in out


def test_phone_check_separates_running_from_reachable(monkeypatch):
    """The failure that looks most like success.

    Started without phone access the dashboard answers on this PC and
    nowhere else -- so everything on the desk works and the phone times out.
    """
    out = run_phone_check(monkeypatch, open_ports=("127.0.0.1",))
    assert "only for this machine" in out
    # Names the address it could not reach, so the report is actionable.
    assert "not on 192.168.1.50" in out


def test_phone_check_gives_the_firewall_command_when_the_rule_is_missing(monkeypatch):
    """The usual reason a correct setup still fails.

    The request never reaches Python, so nothing is logged and the phone
    just spins -- indistinguishable from a wrong address.
    """
    out = run_phone_check(
        monkeypatch, open_ports=("127.0.0.1", "192.168.1.50"), firewall_rule=False,
    )
    assert "netsh advfirewall firewall add rule" in out
    assert "localport=8765" in out
    assert "Administrator" in out


def test_phone_check_ends_on_the_address_to_type(monkeypatch):
    out = run_phone_check(
        monkeypatch, open_ports=("127.0.0.1", "192.168.1.50"), firewall_rule=True,
    )
    assert "http://192.168.1.50:8765/app" in out
    # The one failure that means everything else worked: phones try https
    # for an address typed without a scheme, and the certificate warning
    # reads as "blocked" rather than "wrong address".
    assert "not private" in out
    assert "http://" in out
    # The last thing it can be, once the machine is ruled out.
    assert "separating" in out


def test_phone_check_does_not_ask_windows_questions_elsewhere(monkeypatch):
    out = run_phone_check(
        monkeypatch, open_ports=("127.0.0.1", "192.168.1.50"), platform="linux",
    )
    assert "no firewall rule to check" in out


# ------------------------------------------------- the workspace redesign

# The sidebar used to list every pipeline stage as its own destination,
# which turned a sequence into a menu: nothing said that a brief cannot
# exist before research, or why Build was empty. The stages are now a
# progress navigator inside Campaigns.
#
# These read the built bundle, which is weaker than driving the page and is
# said so plainly -- `scripts/verify/render-check.mjs` is what actually
# renders both the empty and the populated interface under Chromium. What
# this catches is the failure that has happened twice already: a source
# file changed and the committed build left as it was.


def _bundle() -> str:
    return "\n".join(p.read_text() for p in (WEB / "assets").glob("*.js"))


def test_the_four_destinations_ship():
    bundle = _bundle()
    for label in ("Today", "Campaigns", "Contacts", "Results"):
        assert label in bundle, f"the built page has no {label} destination"


def test_the_stages_ship_as_one_sequence():
    bundle = _bundle()
    for step in ("Reachability", "Review & Send"):
        assert step in bundle, f"the stage navigator is missing {step}"
    # The navigator's own element, which is what makes it a sequence rather
    # than five more menu items.
    assert "stage-nav" in bundle


def test_the_page_still_refuses_to_invent_a_follow_up_count():
    """The brief asked for follow-ups due. Nothing records when a reply is
    owed, so the tile says it is not tracked rather than showing a zero --
    which would read as "none due today", a claim rather than a count."""
    bundle = _bundle()
    assert "Not tracked" in bundle


def test_results_says_which_numbers_a_person_sets_by_hand():
    """Replied and Closed are stages a human types into the Sheet. A reply
    rate that quietly means "rows someone remembered to update" is worse
    than none, because it gets believed."""
    bundle = _bundle()
    assert "What is not here" in bundle
