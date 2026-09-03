"""Campaign configuration: YAML for campaign shape, env for secrets.

Nothing region-specific is hardcoded anywhere else in the codebase. Switching
country means pointing CAMPAIGN_CONFIG at a different YAML file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


log = logging.getLogger("outreach")


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
    # anthropic | ollama | ollama-cloud | openrouter | gemini | openai-compatible.
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
    # The model that writes the outreach copy (email + WhatsApp text). Blank
    # means "use research.model". Set it to a smaller model to cut the token
    # cost of drafting -- a short message does not need the big model that
    # summarises reviews. Same provider as research; only the model changes.
    model: str = ""
    # WhatsApp sending via the Baileys sidecar relay.
    whatsapp_enabled: bool = False
    whatsapp_relay_port: int = 4099
    whatsapp_max_per_day: int = 30
    whatsapp_min_interval_seconds: int = 15


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


# Which build targets exist. Kept here, next to research.provider's equivalent
# set, so the two "which backend writes this" choices are validated the same
# way.
BUILD_TARGETS: frozenset[str] = frozenset({"lovable", "firebase"})

# Which backend draws page images. "gemini" is AI Studio's Developer API,
# whose free image quota resets daily and is already exhausted most days.
# "vertex" bills against Cloud trial credits, which do cover it (AI Studio
# does not). Kept separate from BUILD_TARGETS -- the build target is Lovable
# vs. a self-hosted Firebase page; the image provider is orthogonal to that.
IMAGE_PROVIDERS: frozenset[str] = frozenset({"gemini", "vertex"})


@dataclass(frozen=True)
class BuildSettings:
    """The automated Gemini-to-Firebase demo-site path.

    Lovable is not being replaced -- ``target`` defaults to "lovable" and
    ``enabled`` defaults to False, so an existing campaign file that has never
    heard of this block keeps building sites exactly as it does today.
    """

    target: str = "lovable"          # "lovable" | "firebase"
    # The display label + button target for whatever tool Lyon pastes prompts
    # into. A campaign-level craft constant, not a per-business schema field
    # (per CLAUDE.md: craft rules are template text). Defaults match the tool
    # he actually uses today so the live campaign file never has to name it.
    site_builder_name: str = "Google AI Studio"
    site_builder_url: str = "https://aistudio.google.com/"
    enabled: bool = False            # separate from lovable.enabled -- never default true
    provider: str = "gemini"
    model: str = "gemini-3.6-flash"
    # Which model draws the pictures, and how many per page. Images are mood
    # and texture only -- never a depiction of the real premises, which the
    # page prompt and images.py both enforce. 3 keeps a 60-lead run inside
    # the free tier's daily image allowance with room to re-run.
    # Blank means "ask the API which image model this key has". A name typed
    # here is a guess with a shelf life -- this one was wrong three times
    # before it was worth admitting the API already knows. Set it to pin a
    # specific model and skip the lookup.
    image_model: str = ""
    # 0 means "as many as this page needs" -- one per dish, service or
    # highlight the spec named, capped by images.MAX_PER_PAGE. A fixed
    # number was always the wrong shape: it left dishes unillustrated on a
    # rich spec and padded a thin one with filler. Set a number to pin it.
    images_per_page: int = 0
    base_url: str = ""
    # The JSON writers (briefs, specs, outreach copy) cap output at 6000
    # tokens because their replies are a handful of short fields. A full HTML
    # page is not: reusing that cap is the direct cause of a half-written
    # <body> with a truncated final tag, which is worse than no page at all
    # because it can still pass a naive "did it produce output" check.
    # 64k, not 32k. A page with an image per dish, real sections and the
    # motion floor is a big document, and the first real run came back
    # `truncated` -- which is a whole page thrown away, not a small one.
    max_output_tokens: int = 64000
    min_interval_seconds: float = 6.5
    output_dir: str = "sites"
    # Attach a picture of the built site to the WhatsApp message. On by
    # default because a screenshot in a phone notification does far more
    # than a link nobody taps; off is one line for anyone who disagrees.
    screenshot: bool = True
    # Only needed when Edge and Chrome are somewhere this cannot find them.
    screenshot_browser: str = ""
    # Seconds to wait for one page. Deliberately short: the picture is
    # optional and a slow one must never make a run look frozen.
    screenshot_timeout: float = 20.0
    # Where the sites live once AI Studio has pushed them to GitHub: a local
    # clone of that repository, with one folder per business. Jarvis reads
    # from it and never writes to it -- what is in git stays git's.
    sites_repo_dir: str = ""
    # The Cloudflare Pages project every site is published under. One
    # project serves them all, each at its own path.
    cloudflare_project: str = ""
    url_context: bool = False
    public_base_url: str = ""
    max_versions: int = 5
    min_bytes: int = 4000
    max_bytes: int = 400000
    # Which backend draws page images -- see IMAGE_PROVIDERS above.
    image_provider: str = "gemini"
    # Deliberately never auto-discovered the way image_model above is for the
    # Developer API. Vertex prices its image models roughly 5x apart --
    # about $0.045 vs. $0.24 per image on the imagen family at the time this
    # was written -- which on Lyon's ~$260 of Cloud trial credit is the
    # difference between roughly 96 days of images and 18. Auto-picking by
    # sorted(name, reverse=True) or "just take the first one the API lists"
    # would be choosing that trade-off for him without him knowing it was
    # made. So this must be named, on purpose, by whoever is paying.
    vertex_image_model: str = ""
    vertex_region: str = "global"
    # Sent only when non-empty -- leaving it blank means "the model's own
    # default", not "a size named 'default'".
    vertex_image_size: str = ""
    max_images_per_run: int = 60


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
    gemini_api_key: str = ""
    # Cloudflare Pages. The token is a secret and lives in .env; the
    # project and the repo folder are settings and live in the campaign.
    cloudflare_api_token: str = ""
    cloudflare_account_id: str = ""
    firebase_site_id: str = ""
    firebase_project_id: str = ""
    firebase_service_account_file: str = ""
    # A separate OAuth token from token_file above -- see auth.VERTEX_SCOPES
    # for why the Sheets/Gmail token must never be asked to cover this too.
    vertex_token_file: Path = Path("credentials/vertex_token.json")
    vertex_project_id: str = ""


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
    build: BuildSettings = field(default_factory=BuildSettings)
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

    DEFAULT_CAMPAIGN = "config/campaigns/penang-my.yaml"
    from_env = os.getenv("CAMPAIGN_CONFIG")
    path = Path(config_path or from_env or DEFAULT_CAMPAIGN)

    # A CAMPAIGN_CONFIG pointing at a file that is gone must not take the
    # whole tool down. Saving a setting writes that key into .env, aiming it
    # at a *.local.yaml twin -- so if that file is later deleted, moved, or
    # never finished being written, every single command dies at load with
    # "config not found", including ones that have nothing to do with
    # settings. Falling back to the tracked campaign keeps the pipeline
    # usable and says plainly what happened.
    if not path.exists() and not config_path and from_env:
        fallback = Path(DEFAULT_CAMPAIGN)
        if fallback.exists():
            # Named with the value as the environment carried it, not as a
            # Path renders it: the user will search .env for what they typed,
            # and on Windows a normalised path shows backslashes the message
            # then never matches.
            log.warning(
                "CAMPAIGN_CONFIG points at %s, which does not exist. Falling "
                "back to %s -- any settings saved from the dashboard are not "
                "being applied. Save one again to recreate the file, or "
                "remove CAMPAIGN_CONFIG from .env.",
                from_env,
                fallback,
            )
            path = fallback

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
    wa = o.get("whatsapp") or {}
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
        model=str(o.get("model", "")),
        whatsapp_enabled=bool(wa.get("enabled", False)),
        whatsapp_relay_port=int(wa.get("relay_port", 4099)),
        whatsapp_max_per_day=int(wa.get("max_per_day", 30)),
        whatsapp_min_interval_seconds=int(wa.get("min_interval_seconds", 15)),
    )

    lv = raw.get("lovable") or {}
    lovable = LovableSettings(
        enabled=bool(lv.get("enabled", False)),
        require_confirmation=bool(lv.get("require_confirmation", True)),
        auto_approve=bool(lv.get("auto_approve", False)),
    )

    b = raw.get("build") or {}
    build_target = str(b.get("target", "lovable")).strip().lower()
    if build_target not in BUILD_TARGETS:
        raise ConfigError(
            f"build.target must be one of {sorted(BUILD_TARGETS)}, got "
            f"{build_target!r}"
        )
    # Display label + button target for the site-builder tool. The name is
    # required (a blank button label is a broken UI); the URL is allowed to be
    # blank, in which case the dashboard simply omits the "Open [tool]" button
    # rather than rendering a dead link.
    site_builder_name = str(b.get("site_builder_name", "Google AI Studio")).strip()
    if not site_builder_name:
        raise ConfigError(
            "build.site_builder_name is empty. Set it to the tool name you "
            "paste prompts into (e.g. 'Google AI Studio'), or remove the line "
            "to use the default."
        )
    site_builder_url = str(b.get("site_builder_url", "https://aistudio.google.com/")).strip()
    if site_builder_url and not site_builder_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"build.site_builder_url must be a full http(s) URL, got "
            f"{site_builder_url!r}. It's the button that opens your site "
            "builder, e.g. 'https://aistudio.google.com/'."
        )
    if build_target == "firebase" and not os.getenv("FIREBASE_SITE_ID"):
        raise ConfigError(
            "build.target is 'firebase', so FIREBASE_SITE_ID must be set in "
            ".env. Find it in the Firebase console under Hosting -- it's the "
            "site name your demo URLs will be published under."
        )
    image_provider = str(b.get("image_provider", "gemini")).strip().lower()
    if image_provider not in IMAGE_PROVIDERS:
        raise ConfigError(
            f"build.image_provider must be one of {sorted(IMAGE_PROVIDERS)}, "
            f"got {image_provider!r}"
        )
    if image_provider == "vertex" and not os.getenv("VERTEX_PROJECT_ID"):
        raise ConfigError(
            "build.image_provider is 'vertex', so VERTEX_PROJECT_ID must be "
            "set in .env. That's the Project ID from the Google Cloud "
            "Console project picker -- the short text one like "
            "'sme-outreach-482913', not the project number and not the "
            "display name."
        )
    # Deliberately NOT validated at load time, unlike every other required
    # setting in this file. The commands that exist to *find* a model name --
    # `vertex-models` and `vertex-check` -- have to load the config to know
    # the project and region, so refusing to load without a model name means
    # the only way to satisfy the check is a tool that the check forbids you
    # to run. It is enforced where it is used instead: cli._image_client.
    vertex_image_model = str(b.get("vertex_image_model", ""))
    # Guards against "Global" or "global " silently building the wrong host --
    # https://{region}-aiplatform.googleapis.com/... is case-sensitive and
    # whitespace-sensitive, and a typo here fails as a DNS error far from this
    # line rather than as a config error naming the fix.
    vertex_region = str(b.get("vertex_region", "global")).strip().lower()
    max_images_per_run = int(b.get("max_images_per_run", 60))
    if max_images_per_run < 0:
        raise ConfigError(
            f"build.max_images_per_run must be 0 or more, got {max_images_per_run}"
        )

    build = BuildSettings(
        target=build_target,
        site_builder_name=site_builder_name,
        site_builder_url=site_builder_url,
        enabled=bool(b.get("enabled", False)),
        provider=str(b.get("provider", "gemini")),
        model=str(b.get("model", "gemini-3.6-flash")),
        image_model=str(b.get("image_model", "")),
        images_per_page=int(b.get("images_per_page", 0)),
        base_url=str(b.get("base_url", "")),
        max_output_tokens=int(b.get("max_output_tokens", 64000)),
        min_interval_seconds=float(b.get("min_interval_seconds", 6.5)),
        # Resolved relative to CWD, not to this file. Both Dashboard.bat and
        # run.bat do `cd /d "%~dp0"` before invoking Python, so CWD is the
        # repo root by construction on Windows -- the same assumption
        # credentials/oauth_client.json above already relies on.
        output_dir=str(b.get("output_dir", "sites")),
        screenshot=bool(b.get("screenshot", True)),
        screenshot_browser=str(b.get("screenshot_browser", "")),
        screenshot_timeout=float(b.get("screenshot_timeout", 20.0)),
        sites_repo_dir=str(b.get("sites_repo_dir", "")),
        cloudflare_project=str(b.get("cloudflare_project", "")),
        url_context=bool(b.get("url_context", False)),
        public_base_url=str(b.get("public_base_url", "")),
        max_versions=int(b.get("max_versions", 5)),
        min_bytes=int(b.get("min_bytes", 4000)),
        max_bytes=int(b.get("max_bytes", 400000)),
        image_provider=image_provider,
        vertex_image_model=vertex_image_model,
        vertex_region=vertex_region,
        vertex_image_size=str(b.get("vertex_image_size", "")),
        max_images_per_run=max_images_per_run,
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
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        cloudflare_api_token=os.getenv("CLOUDFLARE_API_TOKEN", ""),
        cloudflare_account_id=os.getenv("CLOUDFLARE_ACCOUNT_ID", ""),
        firebase_site_id=os.getenv("FIREBASE_SITE_ID", ""),
        firebase_project_id=os.getenv("FIREBASE_PROJECT_ID", ""),
        firebase_service_account_file=os.getenv(
            "FIREBASE_SERVICE_ACCOUNT_FILE", ""
        ),
        vertex_token_file=Path(
            os.getenv("GOOGLE_VERTEX_TOKEN_FILE", "credentials/vertex_token.json")
        ),
        vertex_project_id=os.getenv("VERTEX_PROJECT_ID", ""),
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
        build=build,
        source_path=path,
    )
