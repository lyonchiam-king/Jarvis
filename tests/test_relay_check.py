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
