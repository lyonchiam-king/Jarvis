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
    # Extra search areas. Google caps a single text query at about 60 results
    # no matter how you paginate, so one query is a finite well -- once you have
    # taken those 60, it returns nothing new however often you run it. Listing
    # neighbouring areas is what lets discovery keep finding businesses on day
    # two, day three, and day thirty.
    areas: tuple[str, ...] = ()

    @property
    def search_query(self) -> str:
        """The text handed to Places Text Search, e.g. 'hair salon in Penang'."""
        return f"{self.category} in {self.location}"

    @property
    def search_queries(self) -> tuple[str, ...]:
        """Every query to walk, in order, until the batch cap is filled.

        Falls back to the single ``location`` query when no areas are listed,
        so an existing campaign file keeps working untouched.
        """
        if not self.areas:
            return (self.search_query,)
        return tuple(f"{self.category} in {area}" for area in self.areas)


@dataclass(frozen=True)
class LimitsSettings:
    """One cap governing how many businesses any single stage run touches.

    Every stage honours this, so `run discovery` and `run research` process at
    most the same number of companies per invocation. Keeping runs bounded is
    what makes the pipeline resumable: you run it again and it picks up the
    next batch.
    """

    batch_size: int = 100


@dataclass(frozen=True)
class ResearchSettings:
    # Claude Opus 5 is the default. Effort is the cost/latency dial -- on this
    # model `medium` is genuinely strong for summarisation work; raise it if
    # the briefs read thin.
    model: str = "claude-opus-5"
    effort: str = "medium"
    max_reviews: int = 8
    social_lookup: bool = True
    email_discovery: bool = True
    request_timeout: float = 15.0
    # Flush partial results to the Sheet every N leads, so an interrupted run
    # never has to redo completed work.
    checkpoint_every: int = 10


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
    # Required in every commercial email under CAN-SPAM, GDPR/ePrivacy, and
    # Australia's Spam Act. Appended to the footer of every draft.
    postal_address: str = ""
    # Appended verbatim so the recipient always has a way out.
    opt_out_note: str = (
        "If you'd rather not hear from me again, just reply "
        "'no thanks' and I won't contact you."
    )
    # Only draft outreach for leads whose demo site is built. The demo is the
    # pitch -- a business that has never had a website cannot picture one, and
    # a link to theirs closes that gap in a way no sentence does. Leave this
    # false to send research-angle emails without a site.
    require_site: bool = False


@dataclass(frozen=True)
class LovableSettings:
    enabled: bool = False
    require_confirmation: bool = True
    # Seed new briefs as Approved instead of Pending. Set this when building a
    # demo for every lead is the deliberate strategy -- per-lead approval is
    # pure friction at that point. Default stays false so nobody discovers a
    # spend they didn't choose.
    auto_approve: bool = False


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
    limits: LimitsSettings = field(default_factory=LimitsSettings)
    research: ResearchSettings = field(default_factory=ResearchSettings)
    source_path: Path = field(default=Path("."))

    @property
    def batch_size(self) -> int:
        """How many businesses a single stage run may process."""
        return self.limits.batch_size

    @property
    def discovery_cap(self) -> int:
        """Discovery stops at the tighter of its own cap and the global one."""
        return min(self.discovery.max_results, self.limits.batch_size)


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
        areas=tuple(str(a).strip() for a in (campaign_raw.get("areas") or []) if str(a).strip()),
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

    lim = raw.get("limits") or {}
    batch_size = int(lim.get("batch_size", 100))
    if batch_size < 1:
        raise ConfigError(f"limits.batch_size must be at least 1, got {batch_size}")
    limits = LimitsSettings(batch_size=batch_size)

    r = raw.get("research") or {}
    effort = str(r.get("effort", "medium")).lower()
    valid_effort = {"low", "medium", "high", "xhigh", "max"}
    if effort not in valid_effort:
        raise ConfigError(
            f"research.effort must be one of {sorted(valid_effort)}, got {effort!r}"
        )
    research = ResearchSettings(
        model=str(r.get("model", "claude-opus-5")),
        effort=effort,
        max_reviews=int(r.get("max_reviews", 8)),
        social_lookup=bool(r.get("social_lookup", True)),
        email_discovery=bool(r.get("email_discovery", True)),
        request_timeout=float(r.get("request_timeout", 15.0)),
        checkpoint_every=max(1, int(r.get("checkpoint_every", 10))),
    )

    o = raw.get("outreach") or {}
    outreach = OutreachSettings(
        sender_name=str(o.get("sender_name", "")),
        sender_title=str(o.get("sender_title", "")),
        company=str(o.get("company", "")),
        channel_priority=tuple(o.get("channel_priority") or ("whatsapp", "email")),
        postal_address=str(o.get("postal_address", "")),
        require_site=bool(o.get("require_site", False)),
        opt_out_note=str(
            o.get("opt_out_note")
            or OutreachSettings.opt_out_note
        ),
    )

    lv = raw.get("lovable") or {}
    lovable = LovableSettings(
        enabled=bool(lv.get("enabled", False)),
        require_confirmation=bool(lv.get("require_confirmation", True)),
        auto_approve=bool(lv.get("auto_approve", False)),
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
        limits=limits,
        research=research,
        source_path=path,
    )
