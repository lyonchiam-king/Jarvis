"""``modelcheck.run`` proves a model can hold every schema before a batch runs.

These tests inject a fake writer rather than reaching a real endpoint -- the
three calls it makes are already exercised against real schemas in
test_providers_llm.py and test_site_brief.py. What is worth pinning here is
that the check runs the same way regardless of which provider the config
names, including gemini, and that nothing it produces ever contains a key.
"""

from dataclasses import replace

from outreach import modelcheck
from outreach.config import load_config
from outreach.providers.llm import OutreachCopy, ResearchBrief, SiteSpec

PENANG = "config/campaigns/penang-my.yaml"


def gemini_config(**research):
    cfg = load_config(PENANG, env_file=None)
    cfg = replace(
        cfg,
        research=replace(
            cfg.research,
            provider="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-2.5-flash",
            **research,
        ),
    )
    return replace(cfg, secrets=replace(cfg.secrets, llm_api_key="gm-secret-value"))


class FakeWriter:
    """Stands in for OpenAICompatibleWriter -- no network, three clean replies."""

    def __init__(self, fail_site_spec: bool = False):
        self.fail_site_spec = fail_site_spec
        self.calls = 0

    def write(self, prompt: str) -> ResearchBrief:
        self.calls += 1
        return ResearchBrief(
            tone="old-school, unhurried",
            services=("cuts", "hot towel shave"),
            price_tier="budget",
            customer_praise="Never rushes",
            customer_pain_points="Nobody answers the phone",
            suggested_website_angle="Booking would stop the phone tag.",
        )

    def write_outreach_copy(self, prompt: str) -> OutreachCopy:
        self.calls += 1
        return OutreachCopy(
            subject="A quick note about your barber shop",
            email_body="Saw the reviews about missed calls -- happy to help.",
            whatsapp_message="Hi! Saw folks struggle to reach you by phone.",
        )

    def write_site_spec(self, prompt: str) -> SiteSpec:
        self.calls += 1
        if self.fail_site_spec:
            from outreach.providers.llm import LLMError

            raise LLMError(
                "reply was missing required field(s): motion_language, micro_interactions"
            )
        return SiteSpec(
            site_goal="Get a booking",
            pages=("Home",),
            hero_headline="Old-school cuts, no wait",
            hero_subcopy="Book ahead and skip the queue.",
            key_sections=("Hero -- hero with overlay -- headline and CTA",),
            primary_cta="Book now",
            visual_direction="Warm, unhurried, barbershop brown",
            visitor="Someone whose usual barber is closed, on a phone",
            proof_points=("4.6 stars from 88 reviews",),
            palette="#2b2118 background, #f5efe6 text, #c8893a accent",
            typography="Fraunces for headings, Inter for body",
            avoid=("No stock photos of models",),
            highlights=("Walk-ins Welcome",),
            showcase_items=("Hot towel shave -- classic, relaxing",),
            interactive_feature="A slot picker that composes a WhatsApp message",
            mobile_bar=("Call -> tel: link",),
            signature_moment="Before/after that wipes as you drag",
            motion_language="200ms ease-out, staggered 40ms",
            micro_interactions=("CTA -> scale down on press",),
        )


def test_modelcheck_runs_against_a_gemini_config():
    """The provider is gemini; the check does not care -- it only calls the
    writer it is handed, which is exactly what make_writer would build."""
    cfg = gemini_config()
    writer = FakeWriter()

    report = modelcheck.run(cfg, writer)

    assert report.provider == "gemini"
    assert report.model == "gemini-2.5-flash"
    assert report.ok is True
    assert writer.calls == 3
    assert all(check.ok for check in report.checks)


def test_modelcheck_reports_a_failing_site_spec_for_gemini():
    cfg = gemini_config()
    writer = FakeWriter(fail_site_spec=True)

    report = modelcheck.run(cfg, writer)

    assert report.ok is False
    failed = [c for c in report.checks if not c.ok]
    assert len(failed) == 1
    assert "motion_language" in failed[0].missing


def test_modelcheck_never_puts_the_key_in_its_own_report():
    """The config carries the Gemini key; nothing writer-report-shaped may echo it."""
    cfg = gemini_config()
    writer = FakeWriter()

    report = modelcheck.run(cfg, writer)

    rendered = "\n".join(report.lines())
    assert cfg.secrets.llm_api_key not in rendered
    assert "gm-secret-value" not in rendered
