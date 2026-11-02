"""The dashboard's half of the infographic track.

The picture is made outside this tool, so these endpoints are the hand-off:
the prompt out, the image back. Most of what matters here is refusal -- what
the dashboard must NOT accept off the wire, and what it must not let through
to a real business.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from outreach import dashboard as dash
from outreach import schema, visual
from outreach.config import load_config
from outreach.sheets import guard_manual_column

PENANG = "config/campaigns/penang-my.yaml"
PNG = "data:image/png;base64," + base64.b64encode(b"pretend png bytes").decode()


def cfg(track="visual", sheet_id="SHEET1"):
    base = load_config(PENANG, env_file=None)
    return replace(
        base,
        outreach=replace(base.outreach, track=track),
        visual=replace(base.visual, signature="by Lyon"),
        secrets=replace(base.secrets, sheet_id=sheet_id),
    )


class FakeSheets:
    """Enforces the manual-column rule, like the real client.

    A fake more permissive than ``SheetsClient`` is how the manual-column bug
    once shipped past a green suite.
    """

    def __init__(self, leads=None, visuals=None):
        self._tabs = {
            schema.LEADS.title: leads or {},
            schema.VISUAL_BRIEFS.title: visuals or {},
            schema.RESEARCH.title: {},
            schema.OUTREACH_LOG.title: {},
            schema.SITE_BRIEFS.title: {},
        }
        self.writes = []
        self.spreadsheet_id = "fake"

    def read_rows(self, tab):
        return self._tabs.get(tab.title, {})

    def update_column(self, tab, column, values_by_key, *, user_edit=False):
        guard_manual_column(tab, column, user_edit=user_edit)
        self.writes.append((tab.title, column, dict(values_by_key), user_edit))
        for key, value in values_by_key.items():
            row = self._tabs[tab.title].get(key)
            if row:
                row[1][column] = value
        return len(values_by_key)

    def upsert(self, tab, rows, *, on_conflict=None):
        from outreach.sheets import UpsertResult

        return UpsertResult(inserted=len(rows))


def brief(lead_id="a", **extra):
    row = {
        "Lead ID": lead_id,
        "Business Name": "Your Nail Crush",
        "Image Prompt": (
            'Wide 16:9 landscape semi-infographic ... for "Your Nail Crush".\n'
            '\nTop headline, large: "3 quick wins for Your Nail Crush". '
            "Beside the headline, an owl, waving.\n"
            '1: "Calls go unanswered" -> "Let them book online"\n'
            '2: "Prices are not visible" -> "A clear price list"\n'
            '\nBottom ribbon in small text: "by Lyon".\n'
        ),
        "Gaps Used": "no_website: Google lists no website for this business",
        "Style": "paper-cut",
        "Layout": "rows",
        "Specific": "yes",
        "Approval Status": "Approved",
        "Image File": "",
        "Image Source": "",
        "Text Confirmed": "",
        "Created At": "2026-01-01T00:00:00Z",
    }
    row.update(extra)
    return {lead_id: (2, row)}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))

    def build(track="visual", leads=None, visuals=None):
        sheets = FakeSheets(leads or {"a": (2, {"Business Name": "Your Nail Crush"})},
                            visuals if visuals is not None else brief())
        monkeypatch.setattr(dash, "_sheets_client", lambda config: sheets)
        client = dash.create_app(cfg(track)).test_client()
        return client, sheets

    return build


# ------------------------------------------------------------------- queue


def test_the_queue_reports_where_each_graphic_is_up_to(app):
    client, _ = app()
    d = client.get("/api/visual/queue").get_json()
    assert d["enabled"] is True
    assert d["counts"]["needs_image"] == 1
    row = d["queue"][0]
    assert row["name"] == "Your Nail Crush"
    assert row["state"] == "needs_image"
    assert row["specific"] is True
    assert "no_website" in row["gaps"]


def test_the_queue_lists_the_exact_words_that_must_be_on_the_picture(app):
    """Lifted back out of the prompt, which is what was actually pasted into
    the image tool -- recomputing from the Sheet would drift the moment a
    prompt was edited by hand."""
    client, _ = app()
    row = client.get("/api/visual/queue").get_json()["queue"][0]
    assert row["expected_text"][0] == "3 quick wins for Your Nail Crush"
    assert "Calls go unanswered  ->  Let them book online" in row["expected_text"]
    assert row["expected_text"][-1] == "by Lyon"


def test_the_original_track_reports_the_panel_off_rather_than_empty(app):
    client, _ = app(track="website")
    d = client.get("/api/visual/queue").get_json()
    assert d["enabled"] is False
    assert d["queue"] == []


# ------------------------------------------------------------------ attach


def test_a_pasted_picture_is_saved_and_recorded(app):
    client, sheets = app()
    d = client.post("/api/visual/attach",
                    json={"lead_id": "a", "data_url": PNG}).get_json()
    assert d["ok"] is True
    assert d["state"] == "needs_check"
    written = {c: v for _t, c, v, _u in sheets.writes}
    assert written["Image File"]["a"].startswith("visuals/")
    assert written["Image Source"]["a"] == "upload"
    assert visual.resolve_image(written["Image File"]["a"]) is not None


def test_attaching_clears_any_previous_confirmation(app):
    """The words on the new picture have not been read. Carrying a tick
    across from the one it replaced would attach an unchecked graphic to a
    real message, which cannot be undone after sending."""
    client, sheets = app(visuals=brief(**{"Text Confirmed": "yes",
                                          "Image File": "visuals/old.png"}))
    client.post("/api/visual/attach", json={"lead_id": "a", "data_url": PNG})
    written = [(c, v) for _t, c, v, _u in sheets.writes]
    assert ("Text Confirmed", {"a": ""}) in written


def test_a_file_that_is_not_an_image_is_refused_by_type(app):
    client, sheets = app()
    bad = "data:text/html;base64," + base64.b64encode(b"<h1>hi").decode()
    d = client.post("/api/visual/attach",
                    json={"lead_id": "a", "data_url": bad}).get_json()
    assert "ok" not in d
    assert "cannot be sent on WhatsApp" in d["error"]
    assert sheets.writes == []


def test_an_oversized_picture_is_refused_with_the_number(app):
    """The relay answers 413 with an HTML error page the client can only
    report as a transport failure, so the refusal happens here instead."""
    client, _ = app()
    huge = "data:image/png;base64," + base64.b64encode(
        b"x" * (visual.MAX_IMAGE_BYTES + 1)
    ).decode()
    d = client.post("/api/visual/attach",
                    json={"lead_id": "a", "data_url": huge}).get_json()
    assert "8MB limit" in d["error"]


def test_a_private_address_is_never_downloaded_from(app):
    """The dashboard runs on Lyon's machine, and on a server it runs inside a
    private network -- a URL typed into the page is a request made from
    there."""
    client, sheets = app()
    for url in ("http://127.0.0.1:8080/a.png",
                "http://169.254.169.254/latest/meta-data/",
                "http://192.168.1.5/x.png",
                "file:///etc/passwd"):
        d = client.post("/api/visual/attach",
                        json={"lead_id": "a", "url": url}).get_json()
        assert "cannot be downloaded" in d["error"], url
    assert sheets.writes == []


def test_attaching_to_a_lead_with_no_prompt_says_what_to_do(app):
    client, _ = app(visuals={})
    d = client.post("/api/visual/attach",
                    json={"lead_id": "a", "data_url": PNG}).get_json()
    assert "no image prompt yet" in d["error"]


def test_attaching_nothing_at_all_asks_for_something(app):
    client, _ = app()
    d = client.post("/api/visual/attach", json={"lead_id": "a"}).get_json()
    assert "Paste the picture" in d["error"]


def test_attach_needs_a_lead(app):
    client, _ = app()
    assert client.post("/api/visual/attach", json={}).status_code == 400


# ----------------------------------------------------------------- confirm


def test_confirming_with_no_picture_is_refused(app):
    """There is nothing to read the words off."""
    client, sheets = app()
    d = client.post("/api/visual/confirm", json={"lead_id": "a"}).get_json()
    assert "no picture to check" in d["error"]
    assert sheets.writes == []


def test_confirming_a_real_picture_makes_it_sendable(app, tmp_path):
    client, sheets = app()
    client.post("/api/visual/attach", json={"lead_id": "a", "data_url": PNG})
    d = client.post("/api/visual/confirm", json={"lead_id": "a"}).get_json()
    assert d["ok"] is True and d["state"] == "ready"
    row = sheets.read_rows(schema.VISUAL_BRIEFS)["a"][1]
    assert visual.confirmed_image(row) is not None


# ------------------------------------------------------------ clear / skip


def test_clearing_forgets_the_picture_but_keeps_the_file(app):
    """It cost a generation. Deleting someone's only copy to save a few
    kilobytes is the wrong trade."""
    client, sheets = app()
    client.post("/api/visual/attach", json={"lead_id": "a", "data_url": PNG})
    stored = sheets.read_rows(schema.VISUAL_BRIEFS)["a"][1]["Image File"]
    on_disk = visual.resolve_image(stored)
    assert on_disk is not None

    d = client.post("/api/visual/clear", json={"lead_id": "a"}).get_json()
    assert d["state"] == "needs_image"
    assert sheets.read_rows(schema.VISUAL_BRIEFS)["a"][1]["Image File"] == ""
    assert on_disk.exists()


def test_skipping_writes_the_manual_column_as_a_user_edit(app):
    """Approval Status is manual; a dashboard action on a row the user just
    clicked is the named exception."""
    client, sheets = app()
    d = client.post("/api/visual/skip",
                    json={"lead_id": "a", "decision": "Skip"}).get_json()
    assert d["approval"] == "Skip"
    tab, column, values, user_edit = sheets.writes[-1]
    assert (tab, column, user_edit) == (schema.VISUAL_BRIEFS.title,
                                        "Approval Status", True)
    assert values == {"a": "Skip"}


def test_a_skip_can_be_put_back(app):
    client, _ = app(visuals=brief(**{"Approval Status": "Skip"}))
    d = client.post("/api/visual/skip",
                    json={"lead_id": "a", "decision": "Approved"}).get_json()
    assert d["approval"] == "Approved"


def test_skip_refuses_a_decision_it_does_not_know(app):
    client, _ = app()
    r = client.post("/api/visual/skip",
                    json={"lead_id": "a", "decision": "Delete everything"})
    assert r.status_code == 400


# --------------------------------------------------------- serving the file


def test_the_graphic_is_served_as_the_picture_for_that_lead(app):
    client, _ = app(leads={"a": (2, {"Lead ID": "a",
                                     "Business Name": "Your Nail Crush"})})
    client.post("/api/visual/attach", json={"lead_id": "a", "data_url": PNG})
    client.post("/api/visual/confirm", json={"lead_id": "a"})
    r = client.get("/api/screenshot/a")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    assert r.data == b"pretend png bytes"


def test_an_unconfirmed_graphic_is_not_served_as_the_message_picture(app):
    """It is not what the message carries, so the queue must not show it as
    attached."""
    client, _ = app(leads={"a": (2, {"Lead ID": "a",
                                     "Business Name": "Your Nail Crush"})})
    client.post("/api/visual/attach", json={"lead_id": "a", "data_url": PNG})
    r = client.get("/api/screenshot/a")
    assert r.status_code == 404


# ------------------------------------------------------------- the setting


def test_the_track_can_be_set_from_the_page(tmp_path, monkeypatch):
    from pathlib import Path

    from outreach.campaign_edit import local_path_for

    src = Path(PENANG)
    path = tmp_path / "campaign.yaml"
    path.write_text(src.read_text())
    monkeypatch.chdir(tmp_path)
    client = dash.create_app(load_config(path, env_file=None)).test_client()

    d = client.post("/api/track", json={
        "track": "both", "signature": "by Lyon, Business Growth Consultant"
    }).get_json()
    assert d["ok"] is True and d["track"] == "both"
    saved = local_path_for(path).read_text()
    assert "track: " in saved and "signature:" in saved
    assert load_config(local_path_for(path), env_file=None).outreach.track == "both"


def test_a_graphics_track_with_no_signature_is_refused_rather_than_saved(
    tmp_path, monkeypatch
):
    """A campaign asking for graphics with nothing to sign them refuses to
    load, so saving the track alone would leave a tool that will not start."""
    from pathlib import Path

    src = Path(PENANG)
    path = tmp_path / "campaign.yaml"
    path.write_text(src.read_text())
    monkeypatch.chdir(tmp_path)
    client = dash.create_app(load_config(path, env_file=None)).test_client()

    d = client.post("/api/track", json={"track": "visual", "signature": ""}).get_json()
    assert "ok" not in d
    assert "signature" in d["error"]


def test_the_rotation_can_be_cut_down_from_the_page(tmp_path, monkeypatch):
    """His one ask: narrow the series once the good ones are known."""
    from pathlib import Path

    from outreach.campaign_edit import local_path_for

    src = Path(PENANG)
    path = tmp_path / "campaign.yaml"
    path.write_text(src.read_text())
    monkeypatch.chdir(tmp_path)
    client = dash.create_app(load_config(path, env_file=None)).test_client()

    d = client.post("/api/visual/rotation", json={
        "styles": ["paper-cut", "watercolour"], "layouts": ["rows"]
    }).get_json()
    assert d["ok"] is True
    assert d["styles"] == ["paper-cut", "watercolour"]
    assert d["layouts"] == ["rows"]
    reloaded = load_config(local_path_for(path), env_file=None)
    assert reloaded.visual.styles == ("paper-cut", "watercolour")


def test_an_unknown_style_is_refused_and_nothing_is_saved(tmp_path, monkeypatch):
    from pathlib import Path

    src = Path(PENANG)
    path = tmp_path / "campaign.yaml"
    path.write_text(src.read_text())
    monkeypatch.chdir(tmp_path)
    client = dash.create_app(load_config(path, env_file=None)).test_client()

    d = client.post("/api/visual/rotation", json={
        "styles": ["watercolor"], "layouts": []
    }).get_json()
    assert "ok" not in d
    assert "watercolor" in d["error"]


# -------------------------------------------------- the full pipeline button


def test_the_full_pipeline_is_unchanged_on_the_original_track():
    """A campaign that pitches a page must run what it always ran."""
    from outreach.dashboard import FULL_RUN, full_run_for

    assert full_run_for(cfg("website")) == FULL_RUN
    assert "visual-brief" not in full_run_for(cfg("website"))


def test_the_full_pipeline_writes_image_prompts_when_the_track_asks():
    """A stage nobody presses never runs -- which is how the reachability
    check shipped and did nothing for weeks."""
    from outreach.dashboard import full_run_for

    assert full_run_for(cfg("visual")) == [
        "discovery", "research", "visual-brief", "outreach-draft",
    ]


def test_the_visual_only_track_does_not_write_site_briefs():
    """That stage's whole output is a page this campaign never links to."""
    from outreach.dashboard import full_run_for

    assert "site-brief" not in full_run_for(cfg("visual"))


def test_both_tracks_run_both_briefs_in_order():
    from outreach.dashboard import full_run_for

    assert full_run_for(cfg("both")) == [
        "discovery", "research", "site-brief", "visual-brief", "outreach-draft",
    ]


def test_every_stage_the_full_pipeline_names_is_a_real_command():
    """A name that is not in STAGE_COMMANDS fails at the subprocess, in a log
    nobody reads, after the earlier stages have already spent money."""
    from outreach.dashboard import STAGE_COMMANDS, full_run_for

    for track in ("website", "visual", "both"):
        for stage in full_run_for(cfg(track)):
            assert stage in STAGE_COMMANDS, (track, stage)


# ------------------------------------------- what a failed link download says


def _download_errors(app, monkeypatch, status, content_type):
    """Drive the download path with a stand-in for requests.get.

    No egress from this container, so the accepting branch cannot be proven
    here -- but every refusal can, and the refusals are what a person reads.
    """
    import requests

    from outreach import visual as visual_lib

    client, _ = app()
    # This container resolves no public host, so is_fetchable_url refuses
    # every address before a download is attempted. Its own refusals are
    # tested directly above; here the address is taken as reachable so the
    # download's answers are the thing under test.
    monkeypatch.setattr(visual_lib, "is_fetchable_url", lambda _url: True)

    class Resp:
        status_code = status
        headers = {"Content-Type": content_type}

        def iter_content(self, _n):
            yield b"x" * 10

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    return client.post(
        "/api/visual/attach",
        json={"lead_id": "a", "url": "https://example.com/thing"},
    ).get_json()


def test_a_link_needing_a_sign_in_says_so_rather_than_a_status_code(
    app, monkeypatch
):
    """The common case, and the one a bare 403 explains worst: an image tool
    serves your own generations behind your Google sign-in, and this download
    carries no sign-in at all."""
    d = _download_errors(app, monkeypatch, 403, "text/html")
    assert "needs you to be signed in" in d["error"]
    assert "paste it in instead" in d["error"]


def test_a_page_address_says_which_link_to_copy(app, monkeypatch):
    """"That is not an image" is true and useless. Naming the right thing to
    copy is the difference between this working and it looking broken."""
    d = _download_errors(app, monkeypatch, 200, "text/html; charset=utf-8")
    assert "address of the page, not of the picture" in d["error"]
    assert "Copy image address" in d["error"]


def test_an_expired_link_says_links_expire(app, monkeypatch):
    d = _download_errors(app, monkeypatch, 404, "text/html")
    assert "gone (404)" in d["error"]


def test_the_page_tells_him_which_link_to_copy():
    """A hint nobody renders is a hint nobody has."""
    from outreach.dashboard import PAGE

    assert "Copy image address" in PAGE


# -------------------------------------------------------- the Open button


def test_the_tool_address_is_offered_but_never_saved_on_his_behalf(app):
    """Nothing in this repo can reach that host to check it, so the value
    sits in front of him before Save rather than being written for him."""
    from outreach.dashboard import SUGGESTED_IMAGE_TOOL_URL

    client, _ = app()
    d = client.get("/api/track").get_json()
    assert d["tool_url"] == "", "a URL was stored that nobody confirmed"
    assert d["tool_url_suggestion"] == SUGGESTED_IMAGE_TOOL_URL
    assert load_config(PENANG, env_file=None).visual.image_tool_url == ""


def test_saving_the_tool_address_turns_on_the_open_button(tmp_path, monkeypatch):
    from pathlib import Path

    from outreach.campaign_edit import local_path_for

    path = tmp_path / "campaign.yaml"
    path.write_text(Path(PENANG).read_text())
    monkeypatch.chdir(tmp_path)
    client = dash.create_app(load_config(path, env_file=None)).test_client()

    d = client.post("/api/track", json={
        "track": "visual", "signature": "by Lyon",
        "tool_name": "Google Flow", "tool_url": "https://example.com/flow",
    }).get_json()
    assert d["ok"] is True
    assert d["tool_url"] == "https://example.com/flow"
    reloaded = load_config(local_path_for(path), env_file=None)
    assert reloaded.visual.image_tool_name == "Google Flow"
    assert reloaded.visual.image_tool_url == "https://example.com/flow"


def test_a_malformed_tool_address_is_refused_and_the_track_is_not_saved(
    tmp_path, monkeypatch
):
    """One file write, so a valid track cannot be stored and then rolled back
    because the address beside it was wrong."""
    from pathlib import Path

    from outreach.campaign_edit import local_path_for

    path = tmp_path / "campaign.yaml"
    path.write_text(Path(PENANG).read_text())
    monkeypatch.chdir(tmp_path)
    client = dash.create_app(load_config(path, env_file=None)).test_client()

    d = client.post("/api/track", json={
        "track": "visual", "signature": "by Lyon",
        "tool_name": "Google Flow", "tool_url": "labs.google",
    }).get_json()
    assert "ok" not in d
    assert "image_tool_url" in d["error"]
    assert not local_path_for(path).exists() or (
        load_config(local_path_for(path), env_file=None).outreach.track == "website"
    )


def test_clearing_the_address_removes_the_button_and_stays_cleared(
    tmp_path, monkeypatch
):
    """A fork inherits any key it does not carry from the template, so a key
    simply deleted would come straight back on the next load."""
    from pathlib import Path

    from outreach.campaign_edit import local_path_for

    path = tmp_path / "campaign.yaml"
    path.write_text(Path(PENANG).read_text())
    monkeypatch.chdir(tmp_path)
    client = dash.create_app(load_config(path, env_file=None)).test_client()

    client.post("/api/track", json={
        "track": "visual", "signature": "by Lyon",
        "tool_name": "Google Flow", "tool_url": "https://example.com/flow",
    })
    client.post("/api/track", json={
        "track": "visual", "signature": "by Lyon",
        "tool_name": "Google Flow", "tool_url": "",
    })
    assert load_config(local_path_for(path), env_file=None).visual.image_tool_url == ""


# ------------------------------------------- the revert path, on all four
#
# Every campaign edit catches ConfigError and puts the file back rather than
# leaving a campaign the pipeline cannot load. In two of these endpoints that
# name was never in scope, so the line raised NameError, the request 500'd,
# and the broken file stayed on disk -- the exact outcome the revert exists to
# prevent, reported to the page as a transport failure. Nothing covered it
# because the revert only runs when a *valid-looking* save produces an
# unloadable file.


@pytest.fixture
def forkable(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "campaign.yaml"
    path.write_text(Path(PENANG).read_text())
    monkeypatch.chdir(tmp_path)
    client = dash.create_app(load_config(path, env_file=None)).test_client()
    return client, path


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("/api/track", {"track": "visual", "signature": "by Lyon"}),
        ("/api/visual/rotation", {"styles": ["paper-cut"], "layouts": ["rows"]}),
        ("/api/angle", {"angle": "conversion", "offers_follow_up": False}),
        ("/api/design-level", {"level": "standard"}),
    ],
)
def test_a_save_that_will_not_load_is_reverted_not_a_crash(
    forkable, monkeypatch, endpoint, payload
):
    from outreach.campaign_edit import local_path_for
    from outreach.config import ConfigError

    client, path = forkable

    def refuse(*_a, **_k):
        raise ConfigError("pretend this campaign no longer loads")

    monkeypatch.setattr(dash, "load_config", refuse)
    r = client.post(endpoint, json=payload)
    assert r.status_code == 200, f"{endpoint} crashed instead of reverting"
    d = r.get_json()
    assert d is not None, f"{endpoint} answered with something that is not JSON"
    assert "Reverted" in (d.get("error") or ""), d
    # And the file on disk is the one that loads, not the one that does not.
    fork = local_path_for(path)
    if fork.exists():
        import yaml

        assert isinstance(yaml.safe_load(fork.read_text()), dict)


# ------------------------------------------------------ a Sheet made earlier
#
# Every schema addition breaks every existing Sheet the same way: the
# spreadsheet was created from the schema as it stood that day and is never
# touched again, so Google answers a read of the new tab with "Unable to parse
# range" -- which reads as a bug in this tool rather than as a Sheet that
# predates the tab. Reported from a real run the day the track shipped.


GOOGLE_PARSE_ERROR = (
    "<HttpError 400 when requesting https://sheets.googleapis.com/v4/"
    "spreadsheets/XYZ/values/%27Visual%20Briefs%27%21A1%3AN?alt=json "
    'returned "Unable to parse range: \'Visual Briefs\'!A1:N". '
    "Details: \"Unable to parse range: 'Visual Briefs'!A1:N\">"
)


class FakeHttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.resp = type("R", (), {"status": status})()


def test_a_missing_tab_is_named_rather_than_reported_as_a_broken_range(
    monkeypatch,
):
    from outreach import sheets as sheets_mod

    monkeypatch.setattr(sheets_mod, "HttpError", FakeHttpError)

    def boom():
        raise FakeHttpError(400, GOOGLE_PARSE_ERROR)

    with pytest.raises(sheets_mod.TabMissing) as caught:
        sheets_mod._with_retry(boom, "read Visual Briefs")
    exc = caught.value
    assert exc.tab == "Visual Briefs"
    message = str(exc)
    assert "Visual Briefs" in message
    assert "Nothing is broken and nothing is lost" in message
    assert "Add the missing tabs" in message
    # Google's own wording must not survive: it is what made this read as a
    # defect in the tool.
    assert "Unable to parse range" not in message
    assert "HttpError" not in message


def test_an_ordinary_bad_request_is_not_relabelled_as_a_missing_tab(monkeypatch):
    """A control. Telling someone to add a tab that is already there is a
    wasted hour, the same reason a permissions 403 is left alone."""
    from outreach import sheets as sheets_mod

    monkeypatch.setattr(sheets_mod, "HttpError", FakeHttpError)

    def boom():
        raise FakeHttpError(400, "Invalid value at 'data.values' (TYPE_STRING)")

    with pytest.raises(sheets_mod.SheetsError) as caught:
        sheets_mod._with_retry(boom, "write Leads")
    assert not isinstance(caught.value, sheets_mod.TabMissing)


def test_the_missing_tab_message_reaches_the_screens_unwrapped():
    """`explain` must let it through verbatim: wrapping it in prose about the
    Sheet buries the one sentence that is the repair."""
    from outreach.sheets import TabMissing, explain

    exc = TabMissing("Your Sheet has no tab yet.\nPress the button.", "Visual Briefs")
    assert explain(exc, robot_email="robot@x.iam.gserviceaccount.com") == str(exc)


def test_the_queue_flags_a_missing_tab_so_the_page_can_offer_the_button(
    app, monkeypatch
):
    """Flagged, not pattern-matched on the wording: a page that decided which
    button to show by searching an error message breaks the next time a
    sentence is improved."""
    from outreach import dashboard as dash_mod
    from outreach.sheets import TabMissing

    client, sheets = app()

    def refuse(_tab):
        raise TabMissing("no such tab", "Visual Briefs")

    monkeypatch.setattr(sheets, "read_rows", refuse)
    d = client.get("/api/visual/queue").get_json()
    assert d["missing_tab"] is True
    assert "no such tab" in d["error"]


def test_an_ordinary_sheet_failure_is_not_flagged_as_a_missing_tab(
    app, monkeypatch
):
    client, sheets = app()

    def refuse(_tab):
        raise RuntimeError("the network went away")

    monkeypatch.setattr(sheets, "read_rows", refuse)
    d = client.get("/api/visual/queue").get_json()
    assert d["missing_tab"] is False


def test_the_repair_adds_only_what_is_missing(app, monkeypatch):
    """`ensure_tabs` adds tabs and rewrites row 1. No data row is touched, so
    pressing it twice costs nothing."""
    client, sheets = app()
    calls = {"ensure": 0}
    present = {t.title for t in schema.ALL_TABS} - {schema.VISUAL_BRIEFS.title}

    def titles():
        return set(present)

    def ensure(_tabs):
        calls["ensure"] += 1
        present.add(schema.VISUAL_BRIEFS.title)

    sheets.existing_tab_titles = titles
    sheets.ensure_tabs = ensure

    d = client.post("/api/repair-tabs").get_json()
    assert d["ok"] is True
    assert d["added"] == [schema.VISUAL_BRIEFS.title]
    assert "Visual Briefs" in d["note"]
    assert calls["ensure"] == 1


def test_repairing_a_healthy_sheet_says_so_rather_than_claiming_work(app):
    client, sheets = app()
    sheets.existing_tab_titles = lambda: {t.title for t in schema.ALL_TABS}
    sheets.ensure_tabs = lambda _tabs: None

    d = client.post("/api/repair-tabs").get_json()
    assert d["ok"] is True
    assert d["added"] == []
    assert "already there" in d["note"]


def test_the_repair_drops_the_cached_reads_that_just_failed(app, monkeypatch):
    """The read cache is keyed per tab, so a panel that just failed on a
    missing tab would go on failing from its own snapshot until the TTL ran
    out -- which is the repair looking like it did nothing."""
    from outreach import dashboard as dash_mod

    client, sheets = app()
    sheets.existing_tab_titles = lambda: set()
    sheets.ensure_tabs = lambda _tabs: None
    dropped = {"n": 0}
    monkeypatch.setattr(
        dash_mod, "_invalidate_sheets_cache",
        lambda: dropped.__setitem__("n", dropped["n"] + 1),
    )
    client.post("/api/repair-tabs")
    assert dropped["n"] == 1


def test_the_repair_refuses_before_there_is_a_sheet_at_all(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    base = load_config(PENANG, env_file=None)
    cfg_no_sheet = replace(base, secrets=replace(base.secrets, sheet_id=""))
    client = dash.create_app(cfg_no_sheet).test_client()
    d = client.post("/api/repair-tabs").get_json()
    assert "Create my Google Sheet" in d["error"]
