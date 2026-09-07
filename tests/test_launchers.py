"""The double-click launchers.

These were written on Linux for a Windows user, so the failure mode is a typo
nobody sees until it fails on their machine with a blank window. These tests
check what can be checked without cmd.exe: that every command a launcher runs
exists in the CLI, that every `goto` lands somewhere, and that the menu it
prints matches the actions it offers.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BATS = [ROOT / "run.bat", ROOT / "Dashboard.bat"]


def cli_commands():
    from outreach.cli import main

    commands = {name: set() for name in main.commands}
    run_group = main.commands.get("run")
    if run_group is not None:
        commands["run"] = set(run_group.commands)
    return commands


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_every_command_a_launcher_runs_actually_exists(path):
    commands = cli_commands()
    text = path.read_text()

    invocations = re.findall(
        r"(?:%OUTREACH%|\"%APP%\"|\"\.venv\\Scripts\\outreach\.exe\")\s+([a-z-]+)"
        r"(?:\s+([a-z-]+))?",
        text,
    )
    assert invocations, f"{path.name} runs no commands at all"

    for top, sub in invocations:
        assert top in commands, f"{path.name}: unknown command `{top}`"
        if top == "run":
            assert sub in commands["run"], f"{path.name}: unknown stage `run {sub}`"


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_every_goto_lands_on_a_real_label(path):
    text = path.read_text()
    labels = {m.lower() for m in re.findall(r"(?m)^:([A-Za-z_][\w]*)", text)}
    builtin = {"eof"}

    for target in re.findall(r"goto\s+:?([A-Za-z_][\w]*)", text):
        assert target.lower() in labels | builtin, (
            f"{path.name}: `goto {target}` has no matching :{target} label"
        )


def test_the_menu_offers_exactly_the_choices_it_handles():
    """A number printed but not handled silently redraws the menu."""
    text = (ROOT / "run.bat").read_text()

    offered = set(re.findall(r"(?m)^echo\s+(\w)\.\s+\S", text))
    handled = set(re.findall(r'if(?:\s+/i)?\s+"%choice%"=="(\w)"', text))

    assert offered == handled, (
        f"printed but not handled: {sorted(offered - handled)}; "
        f"handled but not printed: {sorted(handled - offered)}"
    )


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_launchers_do_not_install_dev_dependencies(path):
    """A user needs the app, not pytest."""
    assert '".[dev]"' not in path.read_text()


def test_the_dashboard_launcher_verifies_the_install_before_running_it():
    """Without this, a failed install ends in 'not recognized as a command'."""
    text = (ROOT / "Dashboard.bat").read_text()
    assert 'if not exist "%APP%"' in text
    assert "installer-log.txt" in text


def test_the_dashboard_launcher_checks_for_configuration():
    text = (ROOT / "Dashboard.bat").read_text()
    assert 'if not exist ".env"' in text
    assert "SETUP NEEDED" in text


def test_no_variable_is_read_inside_the_block_that_sets_it():
    """In cmd.exe, %VAR% inside a parenthesised block expands at parse time.

    Setting a variable and reading it in the same block yields the *old* value
    -- usually empty. It is the classic batch-file bug and it fails silently,
    so the launchers use subroutines instead.
    """
    for path in BATS:
        text = path.read_text()
        depth = 0
        assigned: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if depth > 0:
                for name in re.findall(r"%(\w+)%", stripped):
                    assert name.lower() not in assigned, (
                        f"{path.name}: %{name}% is read inside the same block "
                        f"that sets it -- it will expand empty"
                    )
            for name in re.findall(r'(?i)^\s*set\s+"?(\w+)=', stripped):
                if depth > 0:
                    assigned.add(name.lower())
            depth += stripped.count("(") - stripped.count(")")
            if depth <= 0:
                depth = 0
                assigned.clear()


def test_the_launcher_stops_an_old_relay_before_starting_a_new_one():
    """The cleanup at the end only runs when the dashboard exits normally, so
    closing the window with the X left the relay behind -- and every later run
    added another. Two relays sharing one WhatsApp session take the connection
    from each other, which reads as a relay that keeps dropping."""
    import pathlib

    text = pathlib.Path("Dashboard.bat").read_text()
    kills = [i for i, line in enumerate(text.splitlines()) if "taskkill" in line]
    start = next(
        i for i, line in enumerate(text.splitlines())
        if "start \"WhatsApp Relay\"" in line
    )

    assert any(i < start for i in kills), (
        "nothing stops a relay left over from the last run"
    )
    # /t or the node process survives its cmd wrapper being killed.
    for i in kills:
        assert "/t" in text.splitlines()[i], (
            "taskkill without /t leaves the node process orphaned"
        )


def test_the_launcher_asks_for_phone_access_and_lets_python_decide():
    """The launcher states the intent; the dashboard decides whether it is
    safe. That check used to be a findstr regex against .env here -- and a
    batch regex is untestable from a machine without Windows, so when it was
    wrong it silently passed no flag: no phone access, and no clue why."""
    import pathlib

    text = pathlib.Path("Dashboard.bat").read_text()
    commands = "\n".join(
        line for line in text.splitlines()
        if not line.strip().upper().startswith("REM")
    )

    assert '"%APP%" dashboard --phone' in commands
    # No decision logic left in batch at all.
    assert "findstr" not in commands or "DASHBOARD_PASSWORD" not in commands
    assert "%PHONE%" not in commands
