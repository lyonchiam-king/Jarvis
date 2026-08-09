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


# Where the model runs. Everything except `anthropic` speaks the OpenAI
# chat-completions API, so one client covers all of them.
PROVIDER_DEFAULTS: dict[str, str] = {
    "anthropic": "",                                  # its own SDK
    "ollama": "http://localhost:11434/v1",            # the local daemon
    "ollama-cloud": "https://ollama.com/v1",          # ollama.com, needs a key
    "openrouter": "https://openrouter.ai/api/v1",
    # AI Studio's OpenAI-compatible endpoint. Google's own docs give this with
    # a trailing slash; the join in providers/llm.py normalises that, but the
    # value is written the documented way here so a diff against their docs
    # is obvious.
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openai-compatible": "",                          # bring your own base_url
}

# Which environment variable holds the key for each provider. Local Ollama
# needs none, which is why it is absent here rather than empty.
PROVIDER_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "ollama-cloud": "OLLAMA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai-compatible": "LLM_API_KEY",
}


@dataclass(frozen=True)
class ResearchSettings:
    # Claude Opus 5 is the default. Effort is the cost/latency dial -- on this
    # model `medium` is genuinely strong for summarisation work; raise it if
    # the briefs read thin.
    model: str = "claude-opus-5"
    effort: str = "medium"
    # anthropic | ollama | ollama-cloud | openrouter | openai-compatible.
    # Only Anthropic *guarantees* the response matches the schema; the others
    # are validated and retried once, then the lead is skipped. See llm.py.
    provider: str = "anthropic"
    # Overrides the provider's default endpoint. Required for
    # `openai-compatible`, optional elsewhere (a LAN Ollama box, say).
    base_url: str = ""
    max_reviews: int = 8
    social_lookup: bool = True
    email_discovery: bool = True
    request_timeout: float = 15.0
    # Flush partial results to the Sheet every N leads, so an interrupted run
    # never has to redo completed work.
    checkpoint_every: int = 10
    # Research the businesses with a Facebook or Instagram page before the ones
    # with no web presence at all. They yield a richer brief and a better demo,
    # and they have already decided they want to be findable online. With a
    # batch cap in play this is what your budget gets spent on.
    social_first: bool = True
    # How to order the queue *within* the socials-first split.
    #   sheet    the order they already sit in -- Google's own relevance
    #            ranking, and the order you can see. Predictable.
    #   reviews  most-reviewed first. Richer briefs, but the queue stops
    #            following the Sheet and reads as random.
    order: str = "sheet"


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
    # The key for whichever provider research.provider names. Equals
    # anthropic_api_key on the default provider; empty for local Ollama,
    # which needs none.
    llm_api_key: str = ""


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
    # Two lists have to agree: the domains discovery treats as "social, so this
    # counts as having no website", and the hosts the code can actually name and
    # link. They drifted once -- business.site, carrd.co and linktr.ee were in
    # the campaign and not in the code, so those leads were accepted *because*
    # they were social and then had their only URL silently dropped. Research
    # now skips a lead whose URL it cannot classify, which turns that same drift
    # into lost work, so it fails here instead: at load, naming the domain.
    #
    # Imported inside the function for the same reason selection.ORDERS is --
    # providers.social imports requests, and config must stay cheap to import.
    from .providers.social import platform_for

    unknown = [
        domain
        for domain in (d.get("social_domains") or ())
        if not platform_for(f"https://x.{domain}")
    ]
    if unknown:
        raise ConfigError(
            "discovery.social_domains lists "
            + ", ".join(repr(u) for u in unknown)
            + ", which the code cannot recognise, so leads found there would "
            "be accepted as social and then lose their only link. Add each one "
            "to PLATFORMS in src/outreach/providers/social.py with the name a "
            "person would say (e.g. \"carrd.co\": \"Carrd\"), or remove it here."
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
    from .selection import ORDERS

    order = str(r.get("order", "sheet")).strip().lower()
    if order not in ORDERS:
        raise ConfigError(
            f"research.order must be one of {sorted(ORDERS)}, got {order!r}"
        )

    provider = str(r.get("provider", "anthropic")).strip().lower()
    if provider not in PROVIDER_DEFAULTS:
        raise ConfigError(
            f"research.provider must be one of {sorted(PROVIDER_DEFAULTS)}, "
            f"got {provider!r}"
        )
    base_url = str(r.get("base_url", "") or PROVIDER_DEFAULTS[provider]).rstrip("/")
    if provider == "openai-compatible" and not base_url:
        raise ConfigError(
            "research.provider is 'openai-compatible', so research.base_url "
            "must name the endpoint, e.g. http://192.168.1.10:11434/v1"
        )

    research = ResearchSettings(
        model=str(r.get("model", "claude-opus-5")),
        effort=effort,
        provider=provider,
        base_url=base_url,
        max_reviews=int(r.get("max_reviews", 8)),
        social_lookup=bool(r.get("social_lookup", True)),
        email_discovery=bool(r.get("email_discovery", True)),
        request_timeout=float(r.get("request_timeout", 15.0)),
        checkpoint_every=max(1, int(r.get("checkpoint_every", 10))),
        social_first=bool(r.get("social_first", True)),
        order=order,
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
        llm_api_key=os.getenv(
            PROVIDER_KEY_ENV.get(provider, "ANTHROPIC_API_KEY"), ""
        ),
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
