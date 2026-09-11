"""The morning batch, running on its own.

Every platform schedules jobs differently -- Fly has machines, Render has
cron services, a VPS has crontab -- and each needs its own file and its own
way of failing. Doing it in-process works the same everywhere including
Lyon's PC, and can be driven here rather than only in production.

A scheduled run spends money: Places lookups, research, a brief and a message
per lead. Nobody should find that out from a bill, so it is off unless asked
for, and it stops before anything is built or sent.
"""
from __future__ import annotations

import datetime as dt

import pytest

from outreach import scheduler
from outreach.scheduler import ScheduleError, describe, next_run_at, parse_time


# ----------------------------------------------------------- reading a time


@pytest.mark.parametrize("raw,expected", [
    ("07:30", dt.time(7, 30)),
    (" 07:30 ", dt.time(7, 30)),
    ("00:00", dt.time(0, 0)),
    ("23:59", dt.time(23, 59)),
])
def test_a_time_of_day_is_read(raw, expected):
    assert parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["half seven", "7", "25:00", "07:61", "", "7:30pm"])
def test_nonsense_is_refused_at_startup_not_at_dawn(raw):
    """A typo has to be an error on the way in.

    Validated when it fires, it becomes a morning where nothing happened and
    nothing said why -- and the next clue is 24 hours away.
    """
    with pytest.raises(ScheduleError) as raised:
        parse_time(raw)
    assert "HH:MM" in str(raised.value)


# ----------------------------------------------------------- when it fires


def test_a_time_still_ahead_today_runs_today():
    assert next_run_at(dt.datetime(2026, 9, 11, 6, 0), dt.time(7, 30)) == (
        dt.datetime(2026, 9, 11, 7, 30)
    )


def test_a_time_already_past_runs_tomorrow():
    assert next_run_at(dt.datetime(2026, 9, 11, 9, 0), dt.time(7, 30)) == (
        dt.datetime(2026, 9, 12, 7, 30)
    )


def test_the_exact_minute_counts_as_past():
    """Otherwise a run that has just fired schedules itself again for the
    same instant and loops."""
    assert next_run_at(dt.datetime(2026, 9, 11, 7, 30), dt.time(7, 30)).day == 12


def test_the_clock_time_never_drifts_across_a_clock_change():
    """Adding 24 hours to an aware datetime moves the wall clock by an hour
    where DST applies, so a 07:30 job becomes 06:30 and stays there.

    Penang has no DST; a campaign run from the UK does. Building the next
    run from a date and a time keeps the same o'clock either way.
    """
    from zoneinfo import ZoneInfo

    london = ZoneInfo("Europe/London")
    # The night the UK clocks go back an hour.
    before = dt.datetime(2026, 10, 24, 9, 0, tzinfo=london)

    following = next_run_at(before, dt.time(7, 30))
    assert (following.hour, following.minute) == (7, 30)
    assert following.date() == dt.date(2026, 10, 25)


def test_the_description_spells_out_what_was_computed():
    """A time with no timezone on it is the most reliable way to schedule
    something for the wrong moment -- "07:30" reads as local to whoever set
    it and as UTC to the container, eight hours apart in Penang."""
    line = describe(dt.time(7, 30), dt.datetime(2026, 9, 11, 22, 0).astimezone())
    assert "07:30" in line
    assert "Sat 12 Sep 07:30" in line
    assert "9h 30m" in line


# -------------------------------------------------------- a simulated day


class Clock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self, start: dt.datetime):
        self.at = start

    def now(self) -> dt.datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += dt.timedelta(seconds=seconds)


def drive(clock: Clock, at: dt.time, run, *, ticks: int) -> None:
    """Run the loop for a bounded number of turns, in this thread."""
    remaining = {"n": ticks}

    def forever() -> bool:
        remaining["n"] -= 1
        return remaining["n"] > 0

    scheduler.start(
        at, run, now=clock.now, sleep=clock.sleep, forever=forever
    ).join(timeout=5)


def test_it_fires_once_when_the_time_arrives():
    clock = Clock(dt.datetime(2026, 9, 11, 7, 0))
    fired = []

    drive(clock, dt.time(7, 30), lambda: fired.append(clock.now()) or True, ticks=400)

    assert len(fired) >= 1
    assert fired[0].hour == 7 and fired[0].minute == 30


def test_it_does_not_fire_twice_in_one_day():
    """The whole point of recomputing the target after a run."""
    clock = Clock(dt.datetime(2026, 9, 11, 7, 0))
    fired = []

    # Enough turns to carry well past the firing minute, but not to tomorrow.
    drive(clock, dt.time(7, 30), lambda: fired.append(clock.now()) or True, ticks=60)

    assert len(fired) == 1, [f.isoformat() for f in fired]


def test_a_run_already_in_progress_is_skipped_not_stacked(caplog):
    """Someone pressed the button. A second pipeline on top of theirs would
    spend twice and interleave two sets of output."""
    clock = Clock(dt.datetime(2026, 9, 11, 7, 0))

    drive(clock, dt.time(7, 30), lambda: False, ticks=400)

    assert "already running" in caplog.text


def test_a_failure_to_start_never_kills_the_thread(caplog):
    """A dead thread takes every future run with it, and nothing would say
    so until someone noticed a quiet week."""
    clock = Clock(dt.datetime(2026, 9, 11, 7, 0))
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("the Sheet was unreachable")

    drive(clock, dt.time(7, 30), boom, ticks=3000)

    assert len(calls) >= 2, "it stopped trying after the first failure"
    assert "the Sheet was unreachable" in caplog.text


def test_it_never_sleeps_through_the_target():
    """A container that was paused, or a clock corrected by NTP, moves time
    in ways one long sleep does not notice -- it wakes hours late having
    missed the run."""
    clock = Clock(dt.datetime(2026, 9, 11, 7, 0))
    slept = []
    original = clock.sleep

    def record(seconds):
        slept.append(seconds)
        original(seconds)

    clock.sleep = record
    drive(clock, dt.time(7, 30), lambda: True, ticks=50)

    assert slept, "it never slept at all"
    assert max(slept) <= scheduler.MAX_SLEEP


# -------------------------------------------------------------- switched off


def test_no_variable_means_no_schedule_and_no_spending(monkeypatch, tmp_path):
    """Discovering a scheduled run by finding a bill is not acceptable."""
    from dataclasses import replace

    from outreach import dashboard as dash
    from outreach.config import load_config

    monkeypatch.delenv(scheduler.ENV_VAR, raising=False)
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(config, secrets=replace(config.secrets, dashboard_password="x" * 12))
    monkeypatch.setattr(dash, "load_config", lambda *a, **k: config)

    started = []
    monkeypatch.setattr(scheduler, "start", lambda *a, **k: started.append(a))

    class Stop(Exception):
        pass

    monkeypatch.setattr(dash, "create_app", lambda c: type("A", (), {
        "config": {"state": {}, "runner": None},
        "run": lambda self, **kw: (_ for _ in ()).throw(Stop()),
        "update": lambda self, **kw: None,
    })())

    with pytest.raises(Stop):
        dash.serve(open_browser=False)

    assert started == [], "a schedule was started without being asked for"
