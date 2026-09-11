"""Domain objects shared across stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


def utc_now() -> str:
    """Timestamp format used in every Sheet cell. Sortable as a string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class WebsiteStatus(str, Enum):
    """How much web presence a business already has.

    ``SOCIAL_ONLY`` exists because Google returns a Facebook page in the
    ``websiteUri`` field for a large share of small businesses. Treating that
    as "has a website" would discard the warmest leads in the set -- owners who
    have already decided they want to be findable online.
    """

    NONE = "none"
    SOCIAL_ONLY = "social_only"
    REAL = "real"


@dataclass(frozen=True)
class Lead:
    lead_id: str  # Google place_id
    name: str
    category: str = ""
    address: str = ""
    phone: str = ""
    rating: float | None = None
    review_count: int = 0
    website_status: WebsiteStatus = WebsiteStatus.NONE
    website_url: str = ""
    maps_url: str = ""
    campaign: str = ""
    country: str = ""
    discovered_at: str = ""
    last_refreshed: str = ""

    def to_row(self, stage_value: str) -> dict[str, object]:
        """Map onto Leads-tab column names. Manual columns are absent by design."""
        return {
            "Lead ID": self.lead_id,
            "Stage": stage_value,
            "Business Name": self.name,
            "Category": self.category,
            "Address": self.address,
            "Phone": self.phone,
            "Rating": "" if self.rating is None else self.rating,
            "Review Count": self.review_count,
            "Website Status": self.website_status.value,
            "Website URL": self.website_url,
            "Maps URL": self.maps_url,
            "Campaign": self.campaign,
            "Country": self.country,
            "Discovered At": self.discovered_at,
            "Last Refreshed": self.last_refreshed,
        }
