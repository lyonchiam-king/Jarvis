"""Per-lead error capture.

The pipeline must never die because one lead misbehaved. Stages wrap each lead
in :meth:`ErrorLog.guard`; anything that escapes is recorded and the loop moves
on. Errors are buffered in memory and flushed to the Errors tab in one write at
the end of the run, so a Sheets hiccup cannot itself take the run down.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import requests

from .models import utc_now

log = logging.getLogger("outreach")


@dataclass(frozen=True)
class LeadError:
    timestamp: str
    stage: str
    lead_id: str
    business_name: str
    error_type: str
    message: str
    retryable: bool

    def to_row(self) -> dict[str, object]:
        return {
            "Timestamp": self.timestamp,
            "Stage": self.stage,
            "Lead ID": self.lead_id,
            "Business Name": self.business_name,
            "Error Type": self.error_type,
            "Message": self.message[:900],
            "Retryable": "TRUE" if self.retryable else "FALSE",
        }


def is_retryable(exc: BaseException) -> bool:
    """Whether re-running the stage later has a realistic chance of succeeding.

    Transport failures and server-side/rate-limit responses are retryable; a
    400 or 403 means we sent something wrong or lack permission, and repeating
    it will fail identically.
    """
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    return False


class ErrorLog:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.entries: list[LeadError] = []

    def record(
        self,
        exc: BaseException,
        *,
        lead_id: str = "",
        business_name: str = "",
    ) -> None:
        entry = LeadError(
            timestamp=utc_now(),
            stage=self.stage,
            lead_id=lead_id,
            business_name=business_name,
            error_type=type(exc).__name__,
            message=str(exc) or repr(exc),
            retryable=is_retryable(exc),
        )
        self.entries.append(entry)
        log.warning(
            "[%s] %s failed: %s: %s",
            self.stage,
            business_name or lead_id or "<unknown lead>",
            entry.error_type,
            entry.message,
        )

    @contextmanager
    def guard(self, *, lead_id: str = "", business_name: str = "") -> Iterator[None]:
        """Swallow any per-lead failure so the run continues.

        KeyboardInterrupt and SystemExit deliberately propagate -- if you hit
        Ctrl-C you want the run to stop, not to be logged as a lead error.
        """
        try:
            yield
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 -- deliberate catch-all
            self.record(exc, lead_id=lead_id, business_name=business_name)

    def __len__(self) -> int:
        return len(self.entries)

    def rows(self) -> list[dict[str, object]]:
        return [e.to_row() for e in self.entries]
