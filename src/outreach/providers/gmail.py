"""Gmail drafts.

**This module cannot send email.** It creates drafts and nothing else. There is
no ``drafts().send()`` and no ``messages().send()`` call anywhere in this
codebase, and ``tests/test_outreach_draft.py`` greps the whole source tree on
every test run to keep it that way.

That guarantee is enforced here, in code -- not by the OAuth scope. Google does
not publish a scope that allows creating a draft while forbidding a send;
``gmail.compose`` is the narrowest one that exists and it technically permits
both. So the absence of a send call is the actual safety property.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from email.message import EmailMessage

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger("outreach")


class GmailError(Exception):
    """A draft could not be created."""


@dataclass(frozen=True)
class Draft:
    draft_id: str
    message_id: str

    @property
    def link(self) -> str:
        """Deep link that opens this draft in Gmail on the web."""
        return f"https://mail.google.com/mail/u/0/#drafts?compose={self.message_id}"


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
) -> EmailMessage:
    """Assemble the RFC-822 message. No From header -- Gmail fills it in."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def render_services_block(intro: str, services: tuple[str, ...] | list[str]) -> str:
    """The "other ways I can help" list, worded once and never paraphrased.

    Rendered here rather than asked of the model for the same reason the
    compliance footer is: it is the same three lines for every business, and a
    model told to mention them will reword one, drop one, or invent a fourth
    -- and an invented service is something nobody actually sells.

    Returns "" when there is nothing to list, so a campaign that names no
    services gets the message exactly as it was.
    """
    lines = [str(s).strip() for s in services if str(s).strip()]
    if not lines:
        return ""
    # A middot rather than a hyphen: WhatsApp renders a leading "-" as its own
    # list markup in some clients and swallows the character.
    return "\n".join([intro.strip(), *(f"\u2022 {line}" for line in lines)]).strip()


def with_services(message: str, services_block: str) -> str:
    """The WhatsApp counterpart to ``compose_body``'s footer.

    WhatsApp carries no compliance footer -- it is a conversation, not a
    commercial email -- so the services list is the only thing appended to it,
    after a blank line. Separate from ``compose_body`` because that function
    also adds a signature and a postal address, neither of which belongs in a
    chat message.
    """
    if not services_block.strip():
        return message
    return f"{message.rstrip()}\n\n{services_block.strip()}"


def compose_body(
    *,
    body: str,
    sender_name: str,
    sender_title: str,
    company: str,
    postal_address: str,
    opt_out_note: str,
    services_block: str = "",
) -> str:
    """Append the signature and the compliance footer to the model's copy.

    The footer is assembled here rather than left to the model so it is
    identical in every draft and cannot be paraphrased away: a postal address
    and a working opt-out are legal requirements in most of the jurisdictions
    this pipeline targets, not stylistic choices.
    """
    parts = [body.rstrip()]

    # After the message, before the signature: it is part of the offer, not
    # part of the footer, and a reader who stopped at the website link should
    # still meet it on the way to the name at the bottom.
    if services_block.strip():
        parts.append(services_block.strip())

    signature = [line for line in (sender_name, sender_title, company) if line]
    if signature:
        parts.append("\n".join(signature))

    footer: list[str] = []
    if postal_address:
        footer.append(postal_address)
    if opt_out_note:
        footer.append(opt_out_note)
    if footer:
        parts.append("--\n" + "\n".join(footer))

    return "\n\n".join(parts) + "\n"


class GmailClient:
    """Creates drafts in the authenticated user's own mailbox."""

    def __init__(self, credentials, *, service=None) -> None:
        self._service = service or build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )
        self.draft_count = 0

    def probe(self) -> tuple[bool, str]:
        """Is the Gmail API switched on, and does the token carry the scope?

        Lists at most one existing draft -- reads nothing, writes nothing,
        creates nothing. Catches at preflight the two things that otherwise
        surface only once Stage 5 is already running.
        """
        try:
            self._service.users().drafts().list(userId="me", maxResults=1).execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            detail = str(exc)
            if status == 403 and "SERVICE_DISABLED" in detail:
                return False, (
                    "the Gmail API is not enabled on this Cloud project.\n"
                    "       Enable it, wait ~2 minutes, then re-run:\n"
                    "       https://console.cloud.google.com/apis/library/gmail.googleapis.com"
                )
            if status in (401, 403):
                return False, (
                    "Gmail rejected the token. If you set this up before the "
                    "gmail.compose scope existed, delete credentials/token.json "
                    f"and re-run to re-consent.\n       {detail[:200]}"
                )
            return False, f"unexpected Gmail response (HTTP {status}): {detail[:200]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        return True, "enabled, gmail.compose granted"

    def create_draft(self, *, to: str, subject: str, body: str) -> Draft:
        """Create a draft. It is never sent -- you press send in Gmail."""
        if not to or "@" not in to:
            raise GmailError(f"Refusing to draft to an invalid recipient: {to!r}")
        if not subject.strip():
            raise GmailError("Refusing to draft an email with an empty subject")

        message = build_message(to=to, subject=subject, body=body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        try:
            created = (
                self._service.users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw}})
                .execute()
            )
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            raise GmailError(_explain(status, str(exc))) from exc
        except Exception as exc:  # noqa: BLE001 -- surfaces as a per-lead error
            raise GmailError(f"{type(exc).__name__}: {exc}") from exc

        self.draft_count += 1
        return Draft(
            draft_id=created.get("id", ""),
            message_id=(created.get("message") or {}).get("id", ""),
        )


def _explain(status: int | None, detail: str) -> str:
    if status == 403:
        return (
            "Gmail returned 403. Either the Gmail API is not enabled on the Cloud "
            "project, or the OAuth token predates the gmail.compose scope -- "
            f"delete credentials/token.json and re-authenticate.\n{detail[:300]}"
        )
    if status == 401:
        return (
            "Gmail returned 401. The stored token is no longer valid; delete "
            f"credentials/token.json and re-authenticate.\n{detail[:300]}"
        )
    if status == 429:
        return f"Gmail quota exceeded (429).\n{detail[:300]}"
    return f"Gmail draft creation failed (HTTP {status}): {detail[:300]}"
