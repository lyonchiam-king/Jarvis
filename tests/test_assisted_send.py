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


# ------------------------------------------------- the link, on the page

# Everything above proves the server offers a link. None of it proves anyone
# can press one: `wa_link` was computed, returned in every queue response,
# and rendered by neither front end for a week. That is the same unwired
# call site as the reachability stage with no button -- the code was right
# and unreachable, which from Lyon's side is indistinguishable from absent.


def _page_node(harness: str) -> str:
    """Run the old page's script under node -- the harness lives next door."""
    from test_dashboard import _run_in_node

    return _run_in_node(harness)


QUEUE_STUB = """
const box = fakeEl();
document.querySelector = (s) => (s === '#wa-inline' ? box : fakeEl());
globalThis.fetch = async (url) => {
  if (url === '/api/whatsapp/status') return {json: async () => STATUS};
  if (url === '/api/whatsapp/queue') return {json: async () => ({
    queue: [{lead_id: 'L1', name: 'Kedai Kopi', phone: '+60123456789',
             message: 'hi there', has_shot: false,
             wa_link: 'https://wa.me/60123456789?text=hi%20there'}],
    excluded: [],
  })};
  throw new Error('unexpected ' + url);
};
"""


def test_the_card_offers_the_link_when_there_is_no_relay():
    """The hosted case, which is now the normal one. With no relay there is
    no other way to send at all, so a card that only offers the relay button
    is a dead end."""
    out = _page_node("const STATUS = {connected: false, enabled: true};\n"
                     + QUEUE_STUB + """
    await loadWhatsAppInline();
    console.log(JSON.stringify({html: box.innerHTML}));
    """)
    assert "https://wa.me/60123456789?text=" in out, "no click-to-chat link on the card"
    assert "Send on my phone" in out


def test_the_relay_button_is_absent_when_the_relay_is_not_there():
    """A button that cannot work is worse than no button: it is pressed,
    it fails, and the failure reads as the dashboard being broken."""
    out = _page_node("const STATUS = {connected: false, enabled: true};\n"
                     + QUEUE_STUB + """
    await loadWhatsAppInline();
    console.log(JSON.stringify({html: box.innerHTML}));
    """)
    assert "sendWhatsApp(" not in out


def test_the_relay_button_is_there_when_the_relay_is():
    """The control for the test above -- without it, a card rendering no
    actions at all would pass just as happily."""
    out = _page_node("const STATUS = {connected: true, enabled: true};\n"
                     + QUEUE_STUB + """
    await loadWhatsAppInline();
    console.log(JSON.stringify({html: box.innerHTML}));
    """)
    assert "sendWhatsApp(" in out
    assert "https://wa.me/60123456789?text=" in out


def test_the_link_carries_the_edit_rather_than_the_draft_it_replaced():
    """The message box is editable and the link was built server-side before
    the edit existed. Sending the pre-edit text is silent and unrecoverable:
    the business has already read it."""
    out = _page_node("""
    const m = {lead_id: 'L1', message: 'original',
               wa_link: 'https://wa.me/60123456789?text=original'};
    console.log(JSON.stringify({link: waHandLink(m, 'edited by hand')}));
    """)
    assert "https://wa.me/60123456789?text=edited%20by%20hand" in out


def test_the_link_never_rebuilds_the_number():
    """Everything before the `?` is the server's, byte for byte. A browser
    that assembles a dialling code is how a UK lead went out as
    60447450225230 -- accepted, reported as sent, delivered to nobody."""
    out = _page_node("""
    const m = {lead_id: 'L1', message: 'x',
               wa_link: 'https://wa.me/447450225230?text=x'};
    console.log(JSON.stringify({link: waHandLink(m, 'y')}));
    """)
    assert "https://wa.me/447450225230?text=y" in out
    assert "wa.me/60447450225230" not in out


def test_a_message_with_a_quote_cannot_break_out_of_the_href():
    """The page builds its HTML as strings and `esc` does not escape quotes.
    Everything after `?text=` is percent-encoded, so this holds -- but it
    holds by construction, which is exactly the sort of thing that stops
    being true when someone "simplifies" the encoding."""
    out = _page_node("""
    const m = {lead_id: 'L1', message: 'x', wa_link: 'https://wa.me/60123?text=x'};
    console.log(JSON.stringify({link: waHandLink(m, 'he said "hi" <b>')}));
    """)
    import json as _json
    link = _json.loads(out.strip())["link"]
    assert '"' not in link.split("?text=")[1]
    assert "<" not in link


def test_following_the_link_makes_the_card_ask_afterwards():
    """WhatsApp opens elsewhere and tells this page nothing. Without the
    question the lead stays at the top of the queue and the same business is
    messaged again tomorrow."""
    out = _page_node("""
    globalThis.loadWhatsAppInline = async () => {};
    handOffWhatsApp('L1');
    console.log(JSON.stringify({handed: !!waUi['L1'].handed, busy: waBusy()}));
    """)
    assert '"handed":true' in out
    # And the 30-second refresh must stand aside, or the question is wiped
    # off the card before it can be answered -- the same discard that made
    # pressing Send twice do nothing at all.
    assert '"busy":true' in out


def test_saying_it_sent_records_the_claim():
    out = _page_node("""
    let posted = null;
    globalThis.fetch = async (url, opts) => {
      posted = {url, body: JSON.parse(opts.body)};
      return {json: async () => ({ok: true, status: 'unconfirmed'})};
    };
    globalThis.loadWhatsAppInline = async () => {};
    await markSentWhatsApp('L1');
    console.log(JSON.stringify({posted, sent: !!waUi['L1'].sent, note: waUi['L1'].note}));
    """)
    assert '"url":"/api/whatsapp/mark-sent"' in out
    assert '"lead_id":"L1"' in out
    assert '"sent":true' in out


def test_a_failed_recording_does_not_claim_it_worked():
    """The Sheet write can fail -- it is a network call to Google. Showing
    Sent anyway leaves the lead in the queue with a tick beside it, which
    reads as the dashboard being confused rather than as a failed write."""
    out = _page_node("""
    globalThis.fetch = async () => ({json: async () => ({error: 'Sheet unreachable'})});
    globalThis.loadWhatsAppInline = async () => {};
    await markSentWhatsApp('L1');
    console.log(JSON.stringify({sent: !!waUi['L1'].sent, failed: !!waUi['L1'].failed}));
    """)
    assert '"sent":false' in out
    assert '"failed":true' in out


def test_the_new_page_ships_the_hand_off_too():
    """Weaker than the tests above, and said plainly: this reads the built
    bundle for the words, because nothing here runs the React queue with a
    lead in it -- every count in this container is zero for want of a Sheet.
    What it does catch is the case that actually happened, twice: a source
    file fixed and the committed build left as it was.
    """
    import pathlib

    web = pathlib.Path("src/outreach/web/assets")
    bundle = "\n".join(p.read_text() for p in web.glob("*.js"))
    assert "Send on my phone" in bundle, "rebuild frontend/ -- the built page is stale"
    assert "/api/whatsapp/mark-sent" in bundle
