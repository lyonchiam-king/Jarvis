"""The relay's own JavaScript, read as text.

There is no node test harness here and Baileys is not installed, so this
cannot run the relay. What it can do is hold the one line that broke every
lookup: onWhatsApp() must be handed a phone number, never a JID. That single
character difference -- passing `digits` instead of `jid` -- was the whole
bug, and nothing in the Python suite can see it.
"""

import pathlib
import re
import subprocess

RELAY = pathlib.Path(__file__).parent.parent / "whatsapp-relay" / "server.js"


def source() -> str:
    return RELAY.read_text()


def test_the_relay_still_parses():
    """Same guard as the dashboard page: a syntax error here is silent until
    the process refuses to start."""
    result = subprocess.run(
        ["node", "--check", str(RELAY)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_on_whatsapp_is_called_with_digits_not_a_jid():
    """The bug. onWhatsApp builds its query as "+" + what you pass, so a JID
    becomes "+4479...@s.whatsapp.net" and matches nothing -- every number in
    every country answered "not on WhatsApp"."""
    calls = re.findall(r"onWhatsApp\(([^)]*)\)", source())

    assert calls, "the lookup call went missing"
    for arg in calls:
        assert "jid" not in arg, (
            f"onWhatsApp({arg}) passes a JID; it must be given bare digits"
        )


def test_the_send_path_never_reintroduces_a_country_code():
    """A separate old bug, guarded here because both live in this file: the
    relay used to force "60" onto any number without one, turning a US lead
    into a Malaysian stranger."""
    text = source()
    assert '"60" + digits' not in text
    assert "'60' + digits" not in text


def test_a_check_always_answers_the_request():
    """The check runs inside the shared send queue. An escaping throw would
    hang the HTTP request AND poison that queue for the life of the process,
    silently killing every later send."""
    block = source().split('app.post("/check"', 1)[1].split("app.post(", 1)[0]

    assert "try {" in block and "catch" in block
    assert "headersSent" in block, (
        "the failure path must still answer, or the caller waits for ever"
    )


def test_results_are_matched_by_number_not_by_position():
    """Baileys omits numbers that do not exist, so the first entry of a short
    reply can belong to a different number than the one asked about."""
    block = source().split('app.post("/check"', 1)[1].split("app.post(", 1)[0]

    assert ".find(" in block, "results must be matched, not indexed"
    assert "const [hit]" not in block, "destructuring index 0 is the bug this replaces"


def test_the_relay_hardcodes_no_country_anywhere():
    """Item 1: a new country must never need a code change here.

    The relay has twice grown a Malaysia-shaped assumption -- a forced "60"
    on send, and a numbering rule that decided who was reachable. It knows
    about no country now, and must not learn one: everything arrives already
    dialled by phone.py, which resolves the country per lead.
    """
    # Comments are stripped first: the file deliberately explains the old
    # forced-60 bug, and that history is worth keeping. It is the code that
    # must be free of it.
    code = re.sub(r"//[^\n]*", "", source())
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)

    for banned in ('"60"', "'60'", '"+60'):
        assert banned not in code, f"{banned} reintroduces a country into the relay"


def test_sent_is_only_claimed_with_an_acknowledgement():
    """A send that merely resolved was reported as sent, and reached nobody."""
    block = source().split('app.post("/send"', 1)[1].split("app.post(", 1)[0]

    assert "waitForAck" in block, "the send must wait for WhatsApp to confirm"
    assert '"pending"' in block, "an unacknowledged send must not be called sent"


def test_the_recipient_is_checked_but_the_check_does_not_block():
    """The check is worth making and not worth failing on.

    The queue only offers leads whose cached verdict is already yes, so a
    live "no" at send time contradicts an earlier answer from the same API
    and is more likely a hiccup than the truth. Refusing on it stopped
    sending outright, so it is reported alongside the result instead.
    """
    block = source().split('app.post("/send"', 1)[1].split("app.post(", 1)[0]

    assert "onWhatsApp" in block, "the check is still made"
    assert "did not recognise" in block, "and reported"
    assert "status: \"failed\"" not in block.split("unregistered")[-1][:400]


def test_a_bad_attachment_never_costs_the_message():
    """The relay must fall back to text, not fail the send. The picture is
    decoration; the message is the deliverable."""
    block = source().split('app.post("/send"', 1)[1].split("app.post(", 1)[0]

    assert "caption" in block, "the text must travel with the image"
    assert "sending text only" in block, "a bad image must fall back, not throw"


def test_the_body_limit_is_large_enough_for_a_screenshot():
    """The bug that stopped sending the day pictures were added.

    express.json() defaults to a 100kb body. A text message never came near
    it; a 900x1200 PNG always exceeds it, and base64 adds a third on top. So
    Express rejected the request with 413 before the send route ran, and
    sending -- which had worked perfectly for weeks -- broke the moment a
    message started carrying a picture.
    """
    text = source()

    assert "express.json()" not in text, "the default 100kb limit is too small"
    assert "limit:" in text, "the body limit must be set explicitly"


def test_an_oversized_body_answers_json_not_an_html_error_page():
    """Express's own 413 page is unparseable by the caller, which reported
    it as a transport failure and said nothing about size."""
    text = source()

    assert "entity.too.large" in text
    assert "larger than the relay accepts" in text
