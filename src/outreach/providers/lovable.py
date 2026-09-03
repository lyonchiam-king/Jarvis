"""Lovable integration -- built, wired, and switched off.

Every path through this module is inert unless three separate things are true:

1. ``lovable.enabled: true`` in the campaign config
2. the row's ``Approval Status`` column reads ``Approved`` -- and that column is
   manual, so only you can write it
3. the operator confirms at the prompt (unless ``require_confirmation`` is off)

``create_project`` and ``send_message`` spend Lovable credits, which is the
whole reason for the three gates. With the connector absent, the adapter raises
a message telling you how to connect it rather than failing obscurely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("outreach")

CONNECT_HINT = (
    "The Lovable connector is not available to this pipeline.\n"
    "Connect it at claude.ai > Settings > Connectors (search 'Lovable'), then "
    "pass a transport into LovableAdapter.\n"
    "Until then, leave `lovable.enabled: false` and paste the generated prompt "
    "from the Site Briefs tab into Lovable by hand -- the prompt is the "
    "deliverable, the API call is just convenience."
)


class LovableUnavailable(Exception):
    """Lovable was asked to do something but no transport is wired up."""


class LovableSkipped(Exception):
    """A gate said no. Not an error -- the expected path while disabled."""


@dataclass(frozen=True)
class LovableProject:
    project_id: str
    preview_url: str = ""


class LovableTransport(Protocol):
    """Whatever actually talks to Lovable. Injected, never imported."""

    def create_project(self, *, name: str, prompt: str) -> dict[str, Any]: ...


class LovableAdapter:
    def __init__(
        self,
        *,
        enabled: bool = False,
        require_confirmation: bool = True,
        transport: LovableTransport | None = None,
        confirm: Any = None,
    ) -> None:
        self.enabled = enabled
        self.require_confirmation = require_confirmation
        self._transport = transport
        # Injected so tests never block and the CLI can supply click.confirm.
        self._confirm = confirm
        self.projects_created = 0

    def should_build(self, approval_status: str) -> bool:
        """Both the config switch and your manual Approved must agree."""
        if not self.enabled:
            return False
        return (approval_status or "").strip().casefold() == "approved"

    def create_project(self, *, name: str, prompt: str) -> LovableProject:
        """Spend credits. Only reachable once every gate above has passed."""
        if not self.enabled:
            raise LovableSkipped("lovable.enabled is false")

        if self.require_confirmation:
            if self._confirm is None:
                raise LovableSkipped(
                    "confirmation required but no prompt is available "
                    "(non-interactive run)"
                )
            if not self._confirm(
                f"Create a Lovable project for {name}? This spends credits."
            ):
                raise LovableSkipped("operator declined")

        if self._transport is None:
            raise LovableUnavailable(CONNECT_HINT)

        result = self._transport.create_project(name=name, prompt=prompt)
        self.projects_created += 1
        return LovableProject(
            project_id=str(result.get("id") or result.get("project_id") or ""),
            preview_url=str(result.get("preview_url") or result.get("url") or ""),
        )
