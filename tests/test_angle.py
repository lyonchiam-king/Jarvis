"""Which argument the campaign makes, and everything that follows from it.

Two ways to sell the same work. The original says "you have no website, here
is one". The conversion angle says "people find you and leave without getting
in touch, here is a page built for the one thing you want them to do" -- an
argument that works on a business that already has a site, which is most of
them.

It is one setting rather than three because the three cannot be allowed to
disagree: an angle that argues about enquiries while discovery still throws
away everyone with a website would quietly find nobody to say it to.
"""
from __future__ import annotations

import pathlib
import tempfile
from dataclasses import replace

import pytest

from outreach.campaign_edit import CampaignEditError, rewrite_angle
from outreach.config import ANGLES, ConfigError, load_config
from outreach.dashboard import create_app
from outreach.models import WebsiteStatus
from outreach.providers.llm import outreach_copy_system
from outreach.stages.discovery import website_qualifies

PENANG = "config/campaigns/penang-my.yaml"


def load(text: str):
    path = pathlib.Path(tempfile.mkdtemp()) / "campaign.yaml"
    path.write_text(text)
    return load_config(str(path), env_file=None)


def template() -> str:
    return pathlib.Path(PENANG).read_text()


# ---------------------------------------------------------------- the setting


def test_a_campaign_that_never_heard_of_angles_behaves_as_before():
    """The default cannot change what an existing campaign does."""
    config = load_config(PENANG, env_file=None)
    assert config.outreach.angle == "no-website"
    assert config.outreach.offers_follow_up is False


def test_an_unknown_angle_fails_at_load_with_the_fix_in_the_message():
    text = template().replace("outreach:", "outreach:\n  angle: funnel", 1)
    with pytest.raises(ConfigError) as raised:
        load(text)
    assert "no-website" in str(raised.value) and "conversion" in str(raised.value)


@pytest.mark.parametrize("angle", sorted(ANGLES))
@pytest.mark.parametrize("follow_up", [True, False])
def test_every_combination_survives_a_round_trip(angle, follow_up):
    """Four combinations, and the one that broke was writing False.

    `_replace_scalar` quotes by default, so the flag landed as the *string*
    "false" -- which YAML reads as a string and bool() reads as True. Turning
    the promise off left it on.
    """
    saved = load(rewrite_angle(template(), angle, follow_up))
    assert saved.outreach.angle == angle
    assert saved.outreach.offers_follow_up is follow_up


def test_switching_back_and_forth_never_accumulates_keys():
    text = template()
    for angle, follow_up in [("conversion", True), ("no-website", False), ("conversion", False)]:
        text = rewrite_angle(text, angle, follow_up)
    assert text.count("angle:") == 1
    assert text.count("offers_follow_up:") == 1


def test_the_campaigns_comments_are_not_eaten():
    """The file is the user's, and its comments explain his own settings."""
    text = template()
    assert rewrite_angle(text, "conversion", True).count("#") == text.count("#")


def test_a_nonsense_angle_is_refused_before_anything_is_written():
    with pytest.raises(CampaignEditError):
        rewrite_angle(template(), "funnel", False)


# -------------------------------------------------------------- who is kept


def test_the_no_website_angle_still_turns_away_a_real_website():
    assert website_qualifies(WebsiteStatus.REAL, True, "no-website") is False
    assert website_qualifies(WebsiteStatus.NONE, True, "no-website") is True


def test_the_conversion_angle_keeps_businesses_that_have_a_website():
    """The whole point. Its argument is about enquiries, not about having a
    site at all, so the pool it works on is the one the original throws
    away."""
    for status in WebsiteStatus:
        assert website_qualifies(status, False, "conversion") is True


def test_the_default_argument_is_the_original_one():
    """A caller that has not heard of angles must not silently widen the net
    and start messaging every business in town."""
    assert website_qualifies(WebsiteStatus.REAL, True) is False


# ------------------------------------------------------------- what it says


def test_the_conversion_message_never_asks_if_they_need_a_website():
    """That question competes with every web designer and invites a no from
    anyone who already paid for a page."""
    prompt = outreach_copy_system("conversion")
    assert "need a website" not in prompt
    assert "built a website" not in prompt
    assert "enquir" in prompt


def test_the_conversion_message_is_forbidden_from_insulting_their_site():
    """Someone paid for it, quite possibly them."""
    assert "Never criticise" in outreach_copy_system("conversion")


def test_follow_up_is_only_promised_when_the_campaign_offers_it():
    """An invented service is something nobody sells -- and this one would be
    the reason they replied."""
    off = outreach_copy_system("conversion", offers_follow_up=False)
    on = outreach_copy_system("conversion", offers_follow_up=True)
    assert "Never promise follow-up" in off
    assert "Never promise follow-up" not in on
    assert "automatic message" in on


def test_both_angles_keep_the_rules_that_were_paid_for():
    """The shared rules are shared, not duplicated -- two copies drift the
    moment one is edited."""
    for angle in sorted(ANGLES):
        prompt = outreach_copy_system(angle)
        assert "Never list, name or number the sender's other services" in prompt
        assert "Never invent a price" in prompt
        assert "One ask, and make it small" in prompt


# ------------------------------------------------- the setting reaches the model


def test_both_draft_call_sites_pass_the_campaigns_angle():
    """Run and regenerate, each on its own.

    Three of four `render_lovable_prompt` call sites once went unwired with
    every test still green: the prompt was valid, just not the one the
    campaign asked for. Reading the source is the only way to hold both here
    without standing up two whole pipelines.
    """
    source = pathlib.Path("src/outreach/stages/outreach_draft.py").read_text()
    assert source.count("angle=config.outreach.angle") == 2
    assert source.count("offers_follow_up=config.outreach.offers_follow_up") == 2
    assert source.count("writer.write_outreach_copy(") == 2, (
        "a new call site appeared and must pass the angle too"
    )


# ----------------------------------------------------------------- the page


def configured(**secrets):
    config = load_config(PENANG, env_file=None)
    return replace(config, secrets=replace(config.secrets, **secrets))


def test_the_page_can_read_the_current_angle():
    body = create_app(configured()).test_client().get("/api/angle").get_json()
    assert body["angle"] == "no-website"
    assert sorted(body["angles"]) == sorted(ANGLES)
    assert body["offers_follow_up"] is False


def test_the_page_refuses_an_angle_the_pipeline_does_not_know():
    response = create_app(configured()).test_client().post(
        "/api/angle", json={"angle": "funnel"}
    )
    assert response.status_code == 400


def test_follow_up_cannot_be_promised_by_the_angle_that_never_mentions_it():
    """Saving it there would store a claim no message ever makes, and show up
    later as a setting that did nothing."""
    body = create_app(configured()).test_client().post(
        "/api/angle", json={"angle": "no-website", "offers_follow_up": True}
    ).get_json()
    assert "conversion" in body["error"]


# --------------------------------------------------- what actually gets built


def a_spec():
    from outreach.providers.llm import SiteSpec

    return SiteSpec(
        site_goal="Get more bookings",
        pages=("Home", "About", "Services", "Gallery", "Contact"),
        hero_headline="Headline",
        hero_subcopy="Subcopy",
        key_sections=("one", "two"),
        primary_cta="WhatsApp us",
        visual_direction="",
        visitor="Locals",
        proof_points=(),
        layout_system="",
        palette="",
        typography="",
        avoid=(),
        highlights=(),
        showcase_items=(),
        interactive_feature="",
        mobile_bar=(),
        signature_moment="",
        motion_language="",
        micro_interactions=(),
    )


def rendered(angle="no-website"):
    from outreach.providers.llm import render_lovable_prompt

    return render_lovable_prompt(
        business_name="Kedai Kopi",
        category="cafe",
        location="Penang",
        spec=a_spec(),
        angle=angle,
    )


def test_the_conversion_brief_builds_one_page_not_a_small_website():
    """The message promises one page built for one action.

    A model handed the same spec will happily propose five sections, and a
    five-section site arriving under that message is the pitch contradicting
    itself at the only moment anyone is looking.
    """
    out = rendered("conversion")
    assert "One page. Nothing else." in out
    assert "No navigation bar" in out
    # The spec's own page list is overruled, not obeyed.
    assert "- About" not in out
    assert "- Gallery" not in out


def test_the_no_website_brief_still_builds_the_whole_site():
    out = rendered()
    assert "- About" in out and "- Gallery" in out
    assert "No navigation bar" not in out


def test_the_conversion_brief_does_not_call_it_a_replacement():
    """Someone paid for the site this sits beside, quite possibly them."""
    out = rendered("conversion")
    assert "already has a website" in out
    assert "not a replacement" in out
    assert "never had a website" not in out


def test_the_conversion_brief_puts_the_action_above_the_fold():
    out = rendered("conversion")
    assert "without scrolling" in out
    assert "One action, not three" in out


def test_the_shared_enquiry_rule_survives_in_both():
    """The reason differs by angle; the instruction never does."""
    for angle in ("no-website", "conversion"):
        assert "admin panel" in rendered(angle)


def test_the_conversion_brief_never_argues_with_itself():
    """An earlier attempt told the model to disregard a line it had just
    been given. A prompt that contradicts itself is not one to trust with a
    real business's page."""
    assert "IGNORE" not in rendered("conversion")


def test_adding_an_angle_left_the_original_prompt_untouched():
    """The same promise `design_level` makes: "standard" is the prompt as it
    was. Two snapshot tests caught this when a shared line was reworded."""
    fixture = pathlib.Path("tests/fixtures/lovable_prompt.txt")
    if fixture.exists():
        assert "never had a website" in fixture.read_text()


def test_all_four_brief_call_sites_pass_the_angle():
    """Run, --rewrite, generate_sites and regenerate_one.

    Three of these four once went unwired for `design_level` with the whole
    suite green: the prompt was valid, just not the one the campaign asked
    for. They are counted against each other so a new call site cannot be
    added without one.
    """
    source = pathlib.Path("src/outreach/stages/site_brief.py").read_text()
    assert source.count("render_lovable_prompt(") == 4
    assert source.count("angle=config.outreach.angle") == 4
    assert source.count("design_level=config.build.design_level") == 4
