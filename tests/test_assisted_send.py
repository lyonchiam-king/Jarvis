"""Sending without the relay.

The relay is the one part of this pipeline that cannot move to a server: it
needs a phone linked to a machine that stays awake, and running that in a
data centre puts the number at more risk rather than less. A click-to-chat
link needs none of that -- it opens WhatsApp on whatever device follows it,
with the text already written and the recipient already chosen, and a person
presses send.

Everything expensive stays on the server. Pressing send takes a second.
"""
from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest

from outreach import schema
from outreach import dashboard as dash
from outreach.config import load_config
from outreach.dashboard import create_app
from outreach.providers.whatsapp import WA_SENT_STATUSES, chat_link

PENANG = "config/campaigns/penang-my.yaml"


def configured(**secrets):
    config = load_config(PENANG, env_file=None)
    return replace(config, secrets=replace(config.secrets, **secrets))


# ------------------------------------------------------------------ the link


def test_the_whole_message_survives_the_link():
    """A message is lost at exactly its most interesting point otherwise.

    Blank lines separate the four beats; "&" appears in business names; "#"
    turns the rest of a URL into a fragment the server never sees.
    """
    message = "Hi Ana,\n\nSaw the reviews for Salon & Co.\n\nPage: https://x.co/a#top"
    link = chat_link("+60123456789", message)

    query = parse_qs(urlparse(link).query)
    assert query["text"][0] == message, "the message did not survive the round trip"


def test_the_number_is_digits_only():
    """wa.me rejects a leading +, and silently: the chat simply never opens."""
    assert chat_link("+60123456789", "hi").startswith("https://wa.me/60123456789?")


def test_a_lead_with_no_number_gets_no_link():
    """Rather than a link to wa.me/ that opens WhatsApp on nobody."""
    assert chat_link("", "hi") == ""
    assert chat_link("+", "hi") == ""


def test_the_link_never_invents_a_country_code():
    """Normalising happens once, where the lead's country is known.

    A number normalised twice grows a second country code -- a UK lead went
    out as 60447450225230, which WhatsApp accepts, reports as sent, and
    delivers to nobody.
    """
    national = chat_link("07450225230", "hi")
    assert "60" not in urlparse(national).path, "a dialling code was invented"
    assert urlparse(national).path == "/07450225230"


# --------------------------------------------------------------- the queue


class FakeSheets:
    def __init__(self, outreach, leads, research=None):
        self._outreach, self._leads = outreach, research or {}
        self._leads_rows = leads
        self.writes = []

    def read_rows(self, tab):
        if tab is schema.OUTREACH_LOG:
            return self._outreach
        if tab is schema.LEADS:
            return self._leads_rows
        return self._leads

    def update_column(self, tab, column, values, *, user_edit=False):
        rows = self._outreach if tab is schema.OUTREACH_LOG else self._leads_rows
        found = {k: v for k, v in values.items() if k in rows}
        self.writes.append((tab.title, column, dict(found), user_edit))
        return len(found)


def one_drafted_lead():
    outreach = {"L1": (2, {
        "Business Name": "Kedai Kopi",
        "Channel": "whatsapp",
        "Status": "Drafted",
        "WhatsApp Message": "Hi there,\n\nSaw your reviews.",
        "Drafted At": "2026-01-01T00:00:00Z",
    })}
    leads = {"L1": (2, {
        "Business Name": "Kedai Kopi",
        "Phone": "+60123456789",
        "WhatsApp Reachable": "yes",
    })}
    return outreach, leads


def test_the_queue_carries_a_ready_to_press_link(monkeypatch):
    outreach, leads = one_drafted_lead()
    monkeypatch.setattr(dash, "_sheets_client", lambda c: FakeSheets(outreach, leads))
    client = create_app(configured(sheet_id="s")).test_client()

    queue = client.get("/api/whatsapp/queue").get_json()["queue"]

    assert len(queue) == 1
    link = queue[0]["wa_link"]
    assert link.startswith("https://wa.me/60123456789?")
    assert parse_qs(urlparse(link).query)["text"][0] == outreach["L1"][1]["WhatsApp Message"]


# ---------------------------------------------------- recording what you did


def test_sending_it_yourself_is_recorded_as_a_claim_not_a_fact(monkeypatch):
    """Following a link tells this dashboard nothing at all.

    So the lead would sit at the top of the queue forever, inviting the same
    business to be messaged twice -- and the status has to say that nobody
    watched it happen.
    """
    outreach, leads = one_drafted_lead()
    sheets = FakeSheets(outreach, leads)
    monkeypatch.setattr(dash, "_sheets_client", lambda c: sheets)
    client = create_app(configured(sheet_id="s")).test_client()

    body = client.post("/api/whatsapp/mark-sent", json={"lead_id": "L1"}).get_json()

    assert body == {"ok": True, "status": "unconfirmed"}
    written = {(column, tuple(values)): user_edit
               for _tab, column, values, user_edit in sheets.writes}
    assert ("WhatsApp Status", ("L1",)) in written
    assert ("WhatsApp Sent At", ("L1",)) in written, (
        "without the timestamp the daily cap never counts this send"
    )
    # Manual columns, written because the user pressed a button on a named
    # row -- the one exception the guard names.
    assert all(user_edit for user_edit in written.values())


def test_the_recorded_status_is_one_the_queue_treats_as_sent():
    """`unconfirmed` was once missing from this set, and every send stayed at
    the top of the queue with nothing to show it had gone."""
    assert "unconfirmed" in WA_SENT_STATUSES


def test_a_lead_marked_sent_leaves_the_queue(monkeypatch):
    """The whole point. Asserted through the queue rather than the write, so
    a status the queue does not recognise cannot pass."""
    outreach, leads = one_drafted_lead()
    outreach["L1"][1]["WhatsApp Status"] = "unconfirmed"
    monkeypatch.setattr(dash, "_sheets_client", lambda c: FakeSheets(outreach, leads))
    client = create_app(configured(sheet_id="s")).test_client()

    body = client.get("/api/whatsapp/queue").get_json()

    assert body["queue"] == []
    assert [e["reason"] for e in body["excluded"]] == ["already_sent"]


def test_marking_a_lead_that_is_not_there_says_so(monkeypatch):
    monkeypatch.setattr(dash, "_sheets_client", lambda c: FakeSheets({}, {}))
    client = create_app(configured(sheet_id="s")).test_client()

    body = client.post("/api/whatsapp/mark-sent", json={"lead_id": "nope"}).get_json()

    assert "nope" in body["error"]


def test_marking_without_a_lead_is_refused():
    client = create_app(configured(sheet_id="s")).test_client()
    assert client.post("/api/whatsapp/mark-sent", json={}).status_code == 400
