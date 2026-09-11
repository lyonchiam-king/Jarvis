"""Run the morning batch on its own, once a day, wherever this is deployed.

Every platform has its own way of scheduling a job -- Fly has machines, Render
has cron services, a plain VPS has crontab -- and each needs its own file,
its own vocabulary, and its own way of failing. Doing it inside the process
works identically on all of them and on Lyon's PC, which also means it can be
tested here rather than only in production.

It is **off unless asked for**. A scheduled run spends money: Places lookups,
research calls, a site brief and a message per lead. Nobody should discover
that by finding a bill, so an unset variable schedules nothing at all.

What it runs is exactly what the Full pipeline button runs -- discovery
through drafting. It stops before building and before sending, so the two
steps that either cost the most or reach a real business still wait for a
person.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Callable

log = logging.getLogger("outreach")

ENV_VAR = "OUTREACH_DAILY_RUN"

# Never sleep longer than this in one go. A container that was paused, a
# clock corrected by NTP, or a laptop that was closed all move time in ways a
# single long sleep does not notice -- it would wake hours late having missed
# the run entirely. Waking to recompute costs nothing.
MAX_SLEEP = 900.0


class ScheduleError(ValueError):
    """The time could not be read. Always says what a good one looks like."""


def parse_time(raw: str) -> dt.time:
    """Read "07:30" into a time, or explain why not.

    Validated where it is set rather than at the moment it fires, so a typo
    is an error on startup instead of a morning where nothing happened and
    nothing said why.
    """
    cleaned = (raw or "").strip()
    try:
        hour, _, minute = cleaned.partition(":")
        parsed = dt.time(int(hour), int(minute))
    except (ValueError, TypeError) as exc:
        raise ScheduleError(
            f"{ENV_VAR} is {raw!r}, which is not a time of day.\n"
            'Use 24-hour HH:MM -- for example "07:30" for half past seven '
            "in the morning, in this machine's own timezone."
        ) from exc
    if not cleaned.count(":") == 1:
        raise ScheduleError(f"{ENV_VAR} is {raw!r}. Use HH:MM, such as \"07:30\".")
    return parsed


def next_run_at(now: dt.datetime, at: dt.time) -> dt.datetime:
    """When the run after ``now`` should happen.

    Built by putting the time on a date rather than by adding 24 hours to the
    last one. The difference only shows where clocks change: adding a day to
    an aware datetime across a DST boundary moves the wall-clock time by an
    hour, so a 07:30 job drifts to 06:30 and stays there. Combining a date
    with a time keeps the same o'clock, which is what "every morning at
    half seven" means.
    """
    today = now.replace(
        hour=at.hour, minute=at.minute, second=0, microsecond=0
    )
    if today > now:
        return today
    tomorrow = (now.date() + dt.timedelta(days=1))
    return dt.datetime.combine(tomorrow, at, tzinfo=now.tzinfo)


def describe(at: dt.time, now: dt.datetime) -> str:
    """One line naming the next run, in full, with the offset spelled out.

    A time with no timezone on it is the single most reliable way to schedule
    something for the wrong moment -- "07:30" reads as local to whoever set
    it and as UTC to the container, eight hours apart in Penang. Printing
    what was actually computed removes the argument.
    """
    upcoming = next_run_at(now, at)
    hours, remainder = divmod(int((upcoming - now).total_seconds()), 3600)
    return (
        f"Daily run at {at.strftime('%H:%M')} "
        f"({upcoming.strftime('%Z') or 'local time'}); "
        f"next is {upcoming.strftime('%a %d %b %H:%M')}, "
        f"in {hours}h {remainder // 60}m"
    )


def start(
    at: dt.time,
    run: Callable[[], bool],
    *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone(),
    sleep: Callable[[float], None] = time.sleep,
    forever: Callable[[], bool] = lambda: True,
) -> threading.Thread:
    """Call ``run`` once a day at ``at``, in a background thread.

    ``run`` returns whether it started. A False is expected rather than
    exceptional: a run already in progress means someone pressed the button
    and the scheduler must not stack a second one on top of it. It is logged
    and the day is skipped, never retried in a loop.

    The clock, the sleep and the loop condition are injected so the whole
    thing can be driven in a test without waiting for a real morning.
    """
    def loop() -> None:
        target = next_run_at(now(), at)
        log.info("%s", describe(at, now()))
        while forever():
            remaining = (target - now()).total_seconds()
            if remaining > 0:
                sleep(min(remaining, MAX_SLEEP))
                continue
            try:
                if run():
                    log.info("Daily run started.")
                else:
                    # Almost always the person at the keyboard. Saying which
                    # beats a silent skip that looks like a broken schedule.
                    log.warning("Daily run skipped: something is already running.")
            except Exception as exc:  # noqa: BLE001
                # A thread that dies takes every future run with it, and
                # nothing would say so until someone noticed a quiet week.
                log.error("Daily run failed to start: %s", exc)
            target = next_run_at(now(), at)

    thread = threading.Thread(target=loop, name="outreach-daily", daemon=True)
    thread.start()
    return thread
