"""WhatsApp sending via the Baileys sidecar relay.

The relay (whatsapp-relay/server.js) speaks WhatsApp's multi-device WebSocket
protocol. This module is a thin HTTP client to it — it does not hold a WhatsApp
session itself, and it never sees the user's auth credentials.

The provider interface is abstracted so the relay can be swapped for the
official WhatsApp Cloud API later without changing the dashboard or pipeline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

log = logging.getLogger("outreach")


def is_mobile_number(phone: str) -> bool:
    """True when a phone number is a Malaysian mobile (01X) that can receive WhatsApp.

    Landlines (03, 04, 05...) and anything else return False. Handles the
    formats Google Places returns: "+60 12-3456789", "60123456789", "012-3456789".
    """
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("60"):
        digits = "0" + digits[2:]
    return digits.startswith("01")


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
    status: str  # "sent" | "failed"
    timestamp: str


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

    def send_message(self, phone: str, text: str) -> SendResult:
        """Send a WhatsApp text message to a phone number.

        The relay enforces the daily cap and the minimum interval between sends,
        so this call may block for up to ``min_interval_seconds``.
        """
        if not phone or not phone.strip():
            raise WhatsAppError("phone is required")
        if not text or not text.strip():
            raise WhatsAppError("message text is required")

        try:
            resp = requests.post(
                f"{self._base}/send",
                json={"phone": phone.strip(), "message": text.strip()},
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

        return SendResult(
            message_id=data.get("message_id", ""),
            status=status,
            timestamp=data.get("timestamp", ""),
        )

    # ---------------------------------------------------------------- helpers

    def _get(self, path: str) -> dict:
        resp = requests.get(f"{self._base}{path}", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()
