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


# ------------------------------------------- "Waiting for this message"


def test_the_socket_can_answer_a_retry_receipt():
    """The cause of "Waiting for this message. This may take a while."

    WhatsApp is encrypted per device. A device that cannot decrypt a message
    does not give up -- it sends back a retry receipt asking for that message
    again, and Baileys answers by calling `getMessage(key)` for the original
    content and re-encrypting it. With no `getMessage` there is nothing to
    answer with: the retry goes unfulfilled and the message stays "Waiting
    for this message" for ever, on the recipient's phone and on the sender's
    own. Nothing about the send looks wrong, because encryption is not what
    failed.
    """
    src = source()

    assert "getMessage:" in src, (
        "makeWASocket has no getMessage, so no retry receipt can ever be "
        "answered and undecryptable messages stay stuck for good"
    )
    # And it has to read from somewhere, not be a stub returning undefined.
    assert re.search(r"getMessage:.*sentMessages\.get", src), (
        "getMessage must look the message up in the store"
    )


def test_every_sent_message_is_kept_for_a_retry():
    """A getMessage that reads an empty store answers nothing. The content
    has to be recorded at the moment it is sent."""
    src = source()

    assert re.search(r"rememberMessage\(\s*key\?\.id,\s*result\?\.message", src), (
        "the send path must record what it sent, keyed by message id"
    )


def test_the_retry_store_is_bounded():
    """A retry buffer, not a message archive -- this process runs for weeks."""
    src = source()

    assert "MESSAGE_STORE_MAX" in src
    assert re.search(r"while\s*\(\s*sentMessages\.size\s*>", src), (
        "the store must evict, or a long-running relay grows without limit"
    )


# ------------------------------------------------------ logout / re-pair


def test_logout_deletes_the_saved_session():
    """sock.logout() ends the session at WhatsApp's end and leaves the
    credentials on disk. Skipping the delete is the classic Baileys reset
    bug: useMultiFileAuthState reads the dead session straight back, WhatsApp
    answers 401, and the relay reconnect-loops for ever without ever showing
    a QR -- which looks exactly like a hung relay to whoever pressed the
    button."""
    src = source()

    assert "async function clearSession()" in src
    assert re.search(r"fs\.promises\.rm\(\s*AUTH_DIR", src), (
        "the session folder must actually be deleted"
    )
    logout = src.split('app.post("/logout"', 1)[1]
    assert "clearSession()" in logout, "the logout route must clear the session"


def test_a_failed_delete_is_reported_rather_than_claimed_as_success():
    """If the credentials survive, the relay reconnects to the same account.
    Answering ok:true there gives the user a button that silently does
    nothing."""
    logout = source().split('app.post("/logout"', 1)[1]

    assert "ok: false" in logout, "a failed reset must not report ok:true"
    assert "500" in logout


def test_a_reset_holds_off_the_reconnect_that_would_race_it():
    """The close handler reconnects on disconnect. During a reset that would
    rebuild the credential folder being deleted, and the relay would come
    back paired to the account the user just asked to be rid of."""
    src = source()

    assert "let resetting = false" in src
    assert re.search(r"if\s*\(\s*resetting\s*\)\s*return;", src), (
        "the close handler must stand aside while a reset owns the teardown"
    )


def test_a_logged_out_session_is_wiped_before_reconnecting():
    """A revoked session must be removed, not replayed.

    WhatsApp answers the dead credentials with 401 before the QR flow can
    begin, so while the stale auth folder stays on disk the relay loops
    forever logging that it is "waiting for new QR scan" while never
    offering one -- the dashboard shows nothing, and re-pairing is
    impossible without a hand-wiped session directory.

    The guarantee is unchanged; only the mechanism moved. The wipe used to be
    an inline `fs.rmSync` here and is now `clearSession()`, shared with the
    /logout route -- awaited rather than blocking the event loop, and clearing
    the ack and retry tables alongside the credentials.
    """
    close = source().split('if (connection === "close")', 1)[1].split("});", 1)[0]

    assert "clearSession()" in close, (
        "the logged-out path must wipe the stale auth state before "
        "reconnecting, or the 401 loop never offers a QR"
    )


# ------------------------------- the reset that logged out and then died


def test_a_superseded_socket_can_no_longer_act():
    """The crash behind "logged out, then no QR, then relay not working".

    Sockets do not close on command. `logout()` returns before the WebSocket
    has finished closing, so the old socket's connection.update lands *after*
    the replacement is already connecting. It reached the live handler, was
    read as a fresh "logged out", and deleted the credential folder the new
    socket had just written -- whose next saveCreds then threw ENOENT out of
    an event handler and ended the process.

    Every handler now checks the generation it was built under.
    """
    src = source()

    assert "let generation = 0" in src
    assert "const gen = ++generation" in src, (
        "each connect must claim a generation"
    )
    handlers = re.findall(r"sock\.ev\.on\(\s*\"([a-z.]+)\"", src)
    assert set(handlers) >= {"creds.update", "connection.update", "messages.update"}
    # Every one of them, not just the close handler: a stale creds.update
    # writing into a deleted folder is its own crash.
    for name in handlers:
        block = src.split(f'sock.ev.on("{name}"', 1)[1][:400]
        assert "gen !== generation" in block, (
            f"the {name} handler acts on a socket that may already be replaced"
        )


def test_the_reset_orphans_the_old_socket_before_touching_the_disk():
    """Bumping the generation is what makes the old socket's late close event
    harmless. Doing it after the delete leaves the window open."""
    route = source().split('app.post("/logout"', 1)[1]
    bump = route.index("generation += 1")
    assert bump < route.index("clearSession()"), (
        "the old socket must be orphaned before the session is cleared"
    )
    assert bump < route.index("await dying.logout()"), (
        "logout() emits the close event, so the bump must come first"
    )


def test_saving_credentials_can_never_kill_the_relay():
    """saveCreds throws ENOENT when the folder went away underneath it, and
    an unhandled throw inside an event handler ends the process -- the relay
    just vanishes, which is exactly what "relay not working" was."""
    block = source().split('sock.ev.on("creds.update"', 1)[1][:700]

    assert "try {" in block and "catch" in block, (
        "saveCreds must not be able to throw out of the handler"
    )


def test_the_relay_survives_an_unhandled_error():
    """A relay that degrades beats one that disappears. The window is started
    with `cmd /c`, so a crash closes it and takes the stack trace away; all
    the dashboard could say afterwards was "relay not running"."""
    src = source()

    assert 'process.on("uncaughtException"' in src
    assert 'process.on("unhandledRejection"' in src
    # And it must not re-throw or exit -- staying up is the entire point.
    tail = src.split('process.on("uncaughtException"', 1)[1]
    assert "process.exit" not in tail


def test_the_reset_waits_for_the_new_socket_before_reporting_success():
    """Answering as soon as the old session is gone is how a reset that then
    failed to reconnect looked like success followed by a dead relay."""
    route = source().split('app.post("/logout"', 1)[1]

    assert "await connect();" in route, (
        "the reply must mean a socket exists, not merely that the old one went"
    )
    assert route.index("await connect();") < route.index("ok: true")


def test_a_reconnect_that_fails_is_reported():
    route = source().split('app.post("/logout"', 1)[1]
    assert "could not start a new session" in route


def test_deleting_the_session_retries_for_windows():
    """This runs on Windows, where a file another handle still holds gives
    EBUSY rather than deleting -- usually released a moment later."""
    block = source().split("async function clearSession()", 1)[1][:700]

    # The option, not the word. The comment above it explains why Windows
    # needs this and says "maxRetries" too, so a bare substring check passed
    # with the option deleted.
    assert re.search(r"maxRetries:\s*[1-9]", block), (
        "a reset must not fail over a Windows file lock that clears itself"
    )
    assert re.search(r"retryDelay:\s*[1-9]", block)


def test_the_relay_reports_why_it_is_unhappy():
    """"Relay not running" describes a crash, a wrong port and a relay never
    started identically. /status carries the reason so the page can show it."""
    src = source()

    assert "let lastError" in src
    status = src.split('app.get("/status"', 1)[1].split("});", 1)[0]
    assert "last_error" in status
