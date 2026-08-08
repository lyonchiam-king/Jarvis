"""Campaign configuration: YAML for campaign shape, env for secrets.

Nothing region-specific is hardcoded anywhere else in the codebase. Switching
country means pointing CAMPAIGN_CONFIG at a different YAML file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Configuration is missing or malformed. Always actionable for the user."""


@dataclass(frozen=True)
class CampaignSettings:
    name: str
    category: str
    location: str
    language: str = "en"

    @property
    def search_query(self) -> str:
        """The text handed to Places Text Search, e.g. 'hair salon in Penang'."""
        return f"{self.category} in {self.location}"


@dataclass(frozen=True)
class DiscoverySettings:
    max_results: int = 60
    page_size: int = 20
    min_rating: float = 0.0
    min_review_count: int = 0
    allowed_business_status: tuple[str, ...] = ("OPERATIONAL",)
    treat_social_as_no_website: bool = True
    social_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutreachSettings:
    sender_name: str = ""
    sender_title: str = ""
    company: str = ""
    channel_priority: tuple[str, ...] = ("whatsapp", "email")


@dataclass(frozen=True)
class LovableSettings:
    enabled: bool = False
    require_confirmation: bool = True


@dataclass(frozen=True)
class ComplianceSettings:
    country_code: str = ""
    acknowledged: bool = False


@dataclass(frozen=True)
class Secrets:
    places_api_key: str = ""
    sheet_id: str = ""
    oauth_client_file: Path = Path("credentials/oauth_client.json")
    token_file: Path = Path("credentials/token.json")
    anthropic_api_key: str = ""


@dataclass(frozen=True)
class Config:
    campaign: CampaignSettings
    discovery: DiscoverySettings
    outreach: OutreachSettings
    lovable: LovableSettings
    compliance: ComplianceSettings
    secrets: Secrets
    source_path: Path = field(default=Path("."))


def _require(mapping: dict, key: str, where: str) -> object:
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"Missing required key '{key}' in {where}")
    return mapping[key]


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    env_file: str | os.PathLike[str] | None = ".env",
) -> Config:
    """Load campaign YAML plus environment secrets into a single Config.

    Secrets are never read from YAML -- only from the environment -- so a
    campaign file is always safe to commit.
    """
    if env_file is not None and Path(env_file).exists():
        load_dotenv(env_file)

    path = Path(
        config_path
        or os.getenv("CAMPAIGN_CONFIG")
        or "config/campaigns/penang-my.yaml"
    )
    if not path.exists():
        raise ConfigError(
            f"Campaign config not found: {path}\n"
            "Set CAMPAIGN_CONFIG in .env or pass --config."
        )

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    campaign_raw = raw.get("campaign") or {}
    where = f"{path} -> campaign"
    campaign = CampaignSettings(
        name=str(_require(campaign_raw, "name", where)),
        category=str(_require(campaign_raw, "category", where)),
        location=str(_require(campaign_raw, "location", where)),
        language=str(campaign_raw.get("language", "en")),
    )

    d = raw.get("discovery") or {}
    page_size = int(d.get("page_size", 20))
    if not 1 <= page_size <= 20:
        raise ConfigError(
            f"discovery.page_size must be between 1 and 20 (Places API cap), got {page_size}"
        )
    discovery = DiscoverySettings(
        max_results=int(d.get("max_results", 60)),
        page_size=page_size,
        min_rating=float(d.get("min_rating", 0.0)),
        min_review_count=int(d.get("min_review_count", 0)),
        allowed_business_status=tuple(d.get("allowed_business_status") or ("OPERATIONAL",)),
        treat_social_as_no_website=bool(d.get("treat_social_as_no_website", True)),
        social_domains=tuple(d.get("social_domains") or ()),
    )

    o = raw.get("outreach") or {}
    outreach = OutreachSettings(
        sender_name=str(o.get("sender_name", "")),
        sender_title=str(o.get("sender_title", "")),
        company=str(o.get("company", "")),
        channel_priority=tuple(o.get("channel_priority") or ("whatsapp", "email")),
    )

    lv = raw.get("lovable") or {}
    lovable = LovableSettings(
        enabled=bool(lv.get("enabled", False)),
        require_confirmation=bool(lv.get("require_confirmation", True)),
    )

    c = raw.get("compliance") or {}
    country_code = str(c.get("country_code", "")).upper()
    if not country_code:
        raise ConfigError(
            f"{path} -> compliance.country_code is required. "
            "It drives the outreach-law gate; there is no safe default."
        )
    compliance = ComplianceSettings(
        country_code=country_code,
        acknowledged=bool(c.get("acknowledged", False)),
    )

    secrets = Secrets(
        places_api_key=os.getenv("PLACES_API_KEY", ""),
        sheet_id=os.getenv("SHEET_ID", ""),
        oauth_client_file=Path(
            os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "credentials/oauth_client.json")
        ),
        token_file=Path(os.getenv("GOOGLE_TOKEN_FILE", "credentials/token.json")),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    )

    return Config(
        campaign=campaign,
        discovery=discovery,
        outreach=outreach,
        lovable=lovable,
        compliance=compliance,
        secrets=secrets,
        source_path=path,
    )
