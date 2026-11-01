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
