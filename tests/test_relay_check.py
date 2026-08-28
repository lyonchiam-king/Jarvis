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


def test_the_recipient_is_checked_before_a_message_is_sent():
    """WhatsApp accepts a message for any well-formed JID, so sending blind
    to an unregistered number looks exactly like success."""
    block = source().split('app.post("/send"', 1)[1].split("app.post(", 1)[0]

    assert "onWhatsApp" in block
    assert "not registered on WhatsApp" in block
