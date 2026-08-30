"""WhatsApp sending via the Baileys sidecar relay.

The relay (whatsapp-relay/server.js) speaks WhatsApp's multi-device WebSocket
protocol. This module is a thin HTTP client to it — it does not hold a WhatsApp
session itself, and it never sees the user's auth credentials.

The provider interface is abstracted so the relay can be swapped for the
official WhatsApp Cloud API later without changing the dashboard or pipeline.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from dataclasses import dataclass

import requests

from .. import phone as phone_lib

log = logging.getLogger("outreach")


def is_mobile_number(phone: str, country: str = "") -> bool:
    """True when the number's own numbering plan says it is a mobile.

    Country-aware, and deliberately conservative in the other direction from
    how this started: it used to hardcode Malaysia's 01X rule, so the day the
    campaign moved to New Orleans every US lead came back False and the queue
    emptied. US and Canadian numbers carry nothing that separates mobile from
    fixed line, so no offline function can answer for them.

    A False here therefore means "the plan does not say it is a mobile", NOT
    "unreachable". Callers that need the real answer ask WhatsApp through
    `WhatsAppClient.check_numbers`; `phone.line_type` is the honest version of
    this question and returns "unknown" where that is the truth.
    """
    return phone_lib.line_type(phone, country) == "mobile"


# WhatsApp Status values that mean a message has already gone out. Both the
# send queue and the "WhatsApp to send" count check this set, so the two views
# can never drift apart -- the bug that had the top count show a lead section 4
# had correctly hidden, making a drafted message look like it never landed.
WA_SENT_STATUSES = frozenset({"sent", "delivered", "read"})


class WhatsAppError(Exception):
    """The relay could not send the message."""


@dataclass(frozen=True)
class SendResult:
    message_id: str
    status: str  # "sent" | "unconfirmed" | "failed"
    timestamp: str
    # The digits the relay actually addressed. Surfaced so a wrong number is
    # visible in the Sheet rather than inferred from a message never arriving.
    dialled: str = ""
    # Anything the relay wants the user to know without failing the send.
    note: str = ""


class WhatsAppClient:
    """Talks to the local Baileys relay over HTTP.

    The relay must be running on localhost — it is started by Dashboard.bat
    alongside the Flask process, and it is never exposed to the network.
    """

    def __init__(self, port: int = 4099) -> None:
        self._base = f"http://127.0.0.1:{port}"
        self._timeout = 10.0  # seconds — localhost, so anything longer is dead

    # ------------------------------------------------------------------ status

    def is_connected(self) -> bool:
        """True when the relay has an open WhatsApp WebSocket session."""
        try:
            data = self._get("/status")
            return bool(data.get("connected"))
        except Exception:
            return False

    def get_status(self) -> dict:
        """Full status blob: connected, paired, daily counters."""
        try:
            return self._get("/status")
        except Exception as exc:
            return {"connected": False, "error": f"{type(exc).__name__}: {exc}"}

    # --------------------------------------------------------------------- QR

    def get_qr(self) -> str | None:
        """Base64 PNG data URL of the pairing QR code, or None if already paired."""
        try:
            data = self._get("/qr")
            return data.get("qr") or None
        except Exception:
            return None

    # ------------------------------------------------------------------- send

    def send_message(
        self,
        phone: str,
        text: str,
        country: str = "",
        image_path: str | Path | None = None,
    ) -> SendResult:
        """Send a WhatsApp text message to a phone number.

        The number is normalised to E.164 here, because this is the last place
        that knows the lead's country -- the relay does not, and the one time
        it tried to guess it turned a US number into a Malaysian stranger's.
        A number that cannot be resolved to a country is refused rather than
        sent somewhere plausible.

        The relay enforces the daily cap and the minimum interval between sends,
        so this call may block for up to ``min_interval_seconds``.
        """
        if not phone or not phone.strip():
            raise WhatsAppError("phone is required")
        if not text or not text.strip():
            raise WhatsAppError("message text is required")

        e164 = phone_lib.normalise(phone, country)
        if not e164 or e164.startswith("0"):
            raise WhatsAppError(
                f"Cannot tell which country {phone!r} belongs to, so there is "
                "no safe way to dial it. Set the lead's Country cell, or use "
                "the full international number."
            )
        phone = e164

        body = {"phone": phone.strip(), "message": text.strip()}
        if image_path:
            # A missing or unreadable picture never costs the message: the
            # text alone is the thing being sent, and the screenshot only
            # makes it land better.
            try:
                raw = Path(image_path).read_bytes()
                if raw:
                    body["image"] = base64.b64encode(raw).decode("ascii")
            except OSError as exc:
                log.warning("Could not read %s: %s", image_path, exc)

        try:
            resp = requests.post(
                f"{self._base}/send",
                json=body,
                timeout=self._timeout + 30.0,  # relay may be rate-limiting
            )
            data = resp.json()
        except requests.ConnectionError:
            raise WhatsAppError(
                "WhatsApp relay is not running. Start it with Dashboard.bat "
                "or run: node whatsapp-relay/server.js"
            )
        except Exception as exc:
            raise WhatsAppError(f"Relay request failed: {exc}")

        if resp.status_code == 429:
            raise WhatsAppError(
                f"Daily cap reached: {data.get('error', 'too many messages')}"
            )
        if resp.status_code == 503:
            raise WhatsAppError(
                "WhatsApp is not connected. Scan the QR code in the dashboard "
                "to pair your phone."
            )

        status = data.get("status", "failed")
        if status == "failed":
            raise WhatsAppError(data.get("error", "Unknown relay error"))
        # "unconfirmed" means the relay sent it and WhatsApp has not said
        # anything back yet. That was briefly treated as a failure, which
        # stopped the queue: WhatsApp routinely confirms late, especially a
        # first message to a new contact, so silence is missing evidence
        # rather than evidence of nothing. The lead is marked done and the
        # uncertainty is carried on the result for the page to show.

        return SendResult(
            message_id=data.get("message_id", ""),
            status=status,
            timestamp=data.get("timestamp", ""),
            dialled=data.get("dialled", ""),
            note=data.get("note") or data.get("warning") or "",
        )

    # ------------------------------------------------------------------ check

    def check_raw(self, phones: list[str]) -> dict:
        """The relay's full reply, including what it asked WhatsApp and what
        came back. For `outreach whatsapp-probe` -- when a number is known to
        be on WhatsApp and the pipeline says otherwise, this is the only way
        to see which half is lying without guessing."""
        resp = requests.post(
            f"{self._base}/check",
            json={"phones": [p.strip() for p in phones if p.strip()], "debug": True},
            timeout=self._timeout + 5.0 * max(1, len(phones)),
        )
        return resp.json()

    def check_numbers(self, phones: list[str]) -> dict[str, bool | None]:
        """Ask WhatsApp which of these numbers are registered.

        The one reliable answer for a country whose numbering plan cannot
        separate mobile from landline. Numbers must already be E.164 digits --
        `phone.normalise` does that with the lead's country, which this
        process knows and the relay does not.

        Returns number -> True (registered) / False (not) / None (the lookup
        itself failed). None is not False on purpose: a network hiccup must
        not permanently retire a lead, so callers store it as unknown and try
        again another day.
        """
        wanted = [p.strip() for p in phones if p and p.strip()]
        if not wanted:
            return {}

        try:
            resp = requests.post(
                f"{self._base}/check",
                json={"phones": wanted},
                # The relay paces lookups, so this waits by design.
                timeout=self._timeout + 5.0 * len(wanted),
            )
            data = resp.json()
        except requests.ConnectionError:
            raise WhatsAppError(
                "WhatsApp relay is not running. Start it with Dashboard.bat "
                "or run: node whatsapp-relay/server.js"
            )
        except Exception as exc:
            raise WhatsAppError(f"Relay request failed: {exc}")

        if resp.status_code == 503:
            raise WhatsAppError(
                "WhatsApp is not connected. Scan the QR code in the dashboard "
                "to pair your phone."
            )
        if resp.status_code >= 400:
            raise WhatsAppError(data.get("error", "Relay refused the check"))

        out: dict[str, bool | None] = {}
        for number, result in (data.get("results") or {}).items():
            registered = (result or {}).get("registered")
            out[number] = registered if isinstance(registered, bool) else None
        return out

    # ---------------------------------------------------------------- helpers

    def _get(self, path: str) -> dict:
        resp = requests.get(f"{self._base}{path}", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()
