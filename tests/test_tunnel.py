"""Publishing the dashboard to the internet, and the rules that guard it.

A tunnel removes four separate reasons a phone could not reach the dashboard
over wifi. It also turns "reachable from the house" into "reachable from
anywhere", which changes what the password is for -- so most of this file is
about the refusals rather than the feature.

The tunnel itself is driven against a stand-in for cloudflared. A real one
has never run from this container: GitHub releases is unreachable here, so
the binary cannot even be fetched.
"""
from __future__ import annotations

import os
import stat
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from outreach.config import load_config
from outreach.dashboard import MIN_TUNNEL_PASSWORD, UnsafeBinding, create_app
from outreach.providers.tunnel import Tunnel, TunnelError, find_cloudflared


def fake_cloudflared(tmp_path: Path, body: str) -> Path:
    binary = tmp_path / "cloudflared"
    binary.write_text(textwrap.dedent(body).lstrip())
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary


def configured(**secrets):
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    return replace(config, secrets=replace(config.secrets, **secrets))


# --------------------------------------------------------------- the tunnel


def test_the_published_address_is_read_from_what_cloudflared_prints(tmp_path):
    binary = fake_cloudflared(tmp_path, """
        #!/bin/sh
        echo "INF Thank you for trying Cloudflare Tunnel." >&2
        echo "INF |  https://neat-blue-fox-42.trycloudflare.com  |" >&2
        while true; do sleep 1; done
    """)
    tunnel = Tunnel(8765, binary=binary)
    try:
        assert tunnel.start() == "https://neat-blue-fox-42.trycloudflare.com"
    finally:
        tunnel.stop()


def test_a_missing_binary_says_where_to_get_it(tmp_path):
    tunnel = Tunnel(8765, binary=None, root=tmp_path)
    with pytest.raises(TunnelError) as raised:
        tunnel.start()
    assert "developers.cloudflare.com" in str(raised.value)


def test_cloudflared_dying_immediately_is_reported_not_waited_out(tmp_path):
    """No internet looks like this, and thirty seconds of silence helps nobody."""
    binary = fake_cloudflared(tmp_path, """
        #!/bin/sh
        echo "ERR failed to dial" >&2
        exit 1
    """)
    tunnel = Tunnel(8765, binary=binary)
    with pytest.raises(TunnelError) as raised:
        tunnel.start()
    assert "stopped straight away" in str(raised.value)
    assert "internet" in str(raised.value)


def test_stop_is_safe_to_call_twice(tmp_path):
    """It runs in a finally block during shutdown.

    A throw here would replace whatever actually went wrong with a
    misleading second error.
    """
    binary = fake_cloudflared(tmp_path, """
        #!/bin/sh
        echo "INF |  https://x-y-z.trycloudflare.com  |" >&2
        while true; do sleep 1; done
    """)
    tunnel = Tunnel(8765, binary=binary)
    tunnel.start()
    tunnel.stop()
    tunnel.stop()
    assert tunnel.process is None


def test_a_binary_beside_the_project_is_found(tmp_path):
    """Dropping the .exe in the folder has to be enough.

    Editing PATH is not a step to talk a non-programmer through.
    """
    (tmp_path / "cloudflared.exe").write_text("")
    assert find_cloudflared(tmp_path) == tmp_path / "cloudflared.exe"


# ------------------------------------------------------------ the refusals


class Stop(Exception):
    """Raised in place of app.run, so serve() stops at its last decision."""


class FakeApp:
    def __init__(self, config):
        # Mirrors what create_app exposes: serve() records the tunnel here.
        self.config = {"state": {"config": config, "tunnelled": False}}

    def run(self, **kwargs):
        raise Stop

    def update(self, **kwargs):  # app.config.update in serve()
        self.config.update(kwargs)


def serve_with(monkeypatch, config):
    """Call serve() far enough to reach its decisions, never app.run()."""
    import outreach.dashboard as dash

    monkeypatch.setattr(dash, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(dash, "create_app", lambda c: FakeApp(config))
    return Stop, dash


def test_a_tunnel_is_refused_with_no_password(monkeypatch):
    Stop, dash = serve_with(monkeypatch, configured(dashboard_password=""))
    with pytest.raises(UnsafeBinding) as raised:
        dash.serve(open_browser=False, tunnel=True)
    assert "internet" in str(raised.value)
    assert "DASHBOARD_PASSWORD" in str(raised.value)


def test_a_tunnel_is_refused_with_a_short_password(monkeypatch):
    """Good enough for your own wifi is not good enough for a public address.

    New hostnames are scanned within minutes of appearing, and behind this
    one is a tool that spends money and messages real businesses.
    """
    short = "x" * (MIN_TUNNEL_PASSWORD - 1)
    Stop, dash = serve_with(monkeypatch, configured(dashboard_password=short))
    with pytest.raises(UnsafeBinding) as raised:
        dash.serve(open_browser=False, tunnel=True)
    assert str(MIN_TUNNEL_PASSWORD) in str(raised.value)


def test_a_long_password_is_allowed_through_to_the_tunnel(monkeypatch):
    """The refusals must not be the only thing that works."""
    import outreach.providers.tunnel as tunnel_mod

    Stop, dash = serve_with(
        monkeypatch, configured(dashboard_password="a-long-enough-password"),
    )

    class FakeTunnel:
        def __init__(self, port, **kw):
            pass

        def start(self):
            return "https://fake.trycloudflare.com"

        def stop(self):
            pass

    monkeypatch.setattr(tunnel_mod, "Tunnel", FakeTunnel)
    with pytest.raises(Stop):
        dash.serve(open_browser=False, tunnel=True)


# ---------------------------------------------------- who a request is from


def test_login_attempts_are_counted_against_the_real_visitor():
    """Behind a tunnel every request arrives from Cloudflare.

    Keyed on that, one attacker would spend the whole login allowance and
    lock the owner out of their own dashboard.
    """
    app = create_app(configured(dashboard_password="a-long-enough-password"))
    app.config["state"]["tunnelled"] = True
    client = app.test_client()

    for _ in range(9):
        client.post("/login", data={"password": "wrong"},
                    headers={"CF-Connecting-IP": "203.0.113.9"})

    # The attacker is now locked out.
    blocked = client.post("/login", data={"password": "wrong"},
                          headers={"CF-Connecting-IP": "203.0.113.9"})
    assert b"Too many attempts" in blocked.data

    # The owner, arriving from somewhere else, is not.
    mine = client.post("/login", data={"password": "wrong"},
                       headers={"CF-Connecting-IP": "198.51.100.4"})
    assert b"Too many attempts" not in mine.data


def test_the_forwarded_address_is_ignored_when_there_is_no_tunnel():
    """Otherwise the header is a free pass around the rate limit.

    Served on a LAN, anyone could send a different made-up value with every
    request and never exhaust an allowance.
    """
    app = create_app(configured(dashboard_password="a-long-enough-password"))
    assert app.config["state"]["tunnelled"] is False
    client = app.test_client()

    for index in range(9):
        client.post("/login", data={"password": "wrong"},
                    headers={"CF-Connecting-IP": f"203.0.113.{index}"})

    blocked = client.post("/login", data={"password": "wrong"},
                          headers={"CF-Connecting-IP": "203.0.113.200"})
    assert b"Too many attempts" in blocked.data


def test_the_session_cookie_is_locked_down():
    app = create_app(configured(dashboard_password="a-long-enough-password"))
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    # Not Secure by default: a Secure cookie is never sent over plain http,
    # which would silently break the local page. serve() sets it under a
    # tunnel, where every request is https.
    assert not app.config.get("SESSION_COOKIE_SECURE")


# --------------------------------------------------------------- the launcher


LAUNCHER = Path("Dashboard-Anywhere.bat")


def launcher_code() -> str:
    """The launcher with its comments stripped.

    The comments explain the traps and name the very things the assertions
    forbid, so asserting over them matches prose rather than code.
    """
    return "\n".join(
        line for line in LAUNCHER.read_text().splitlines()
        if not line.strip().upper().startswith("REM")
    )


def test_the_launcher_asks_for_a_tunnel_and_decides_nothing():
    """Batch asks; Python answers.

    A findstr against .env decided phone access once, was untestable from a
    machine without Windows, and when wrong did nothing at all -- no flag, no
    access, and no line of output saying why.
    """
    code = launcher_code()
    assert "--tunnel" in code
    assert "findstr" not in code
    assert "DASHBOARD_PASSWORD" not in code, "the launcher must not read secrets"


def test_the_launcher_kills_the_old_relay_tree_first():
    """Two relays on one WhatsApp session take the connection from each other."""
    code = launcher_code()
    assert code.count("taskkill") >= 2, "must clear the old relay before and after"
    assert "/t" in code, "without /t the node process outlives its cmd wrapper"


def test_the_launcher_expands_no_variable_inside_a_block():
    """A batch `if` block expands %VAR% when the block is parsed, not when it runs."""
    for line in launcher_code().splitlines():
        stripped = line.strip()
        if stripped.startswith("if ") and stripped.endswith("("):
            assert "%" not in stripped, line
