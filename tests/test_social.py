"""Reading a business's social profiles: which platform, and the actual link.

A bare handle is not enough for a site builder -- "@espariohair" could be on
any platform, and guessing wrong sends the salon's customers to a stranger.
These tests pin platform, handle and URL as three separate facts.
"""

def test_platform_is_named_not_guessed():
    from outreach.providers.social import platform_for

    assert platform_for("https://facebook.com/x") == "Facebook"
    assert platform_for("https://www.instagram.com/x") == "Instagram"
    assert platform_for("https://m.facebook.com/x") == "Facebook"
    assert platform_for("https://tiktok.com/@x") == "TikTok"
    assert platform_for("https://twitter.com/x") == "X"
    assert platform_for("https://example.com/x") == ""
    assert platform_for("") == ""


def test_a_real_facebook_page_keeps_its_id_in_the_url_but_not_the_handle():
    """The exact shape seen in the wild: a page with no vanity URL."""
    from outreach.providers.social import classify_social_url

    link = classify_social_url(
        "https://www.facebook.com/Espariohairsalon-107771601074381"
    )

    assert link.platform == "Facebook"
    assert link.handle == "@Espariohairsalon", "the numeric id is noise to a reader"
    assert link.url == "https://www.facebook.com/Espariohairsalon-107771601074381"
    assert link.display() == (
        "Facebook @Espariohairsalon "
        "https://www.facebook.com/Espariohairsalon-107771601074381"
    )


def test_a_profile_with_no_readable_handle_is_still_kept():
    """Dropping it would lose the lead's only web presence."""
    from outreach.providers.social import classify_social_url

    link = classify_social_url("https://www.facebook.com/profile.php?id=61550")

    assert link.platform == "Facebook"
    assert link.handle == ""
    assert "id=61550" in link.url, "the id is the only thing identifying the page"


def test_tracking_query_strings_are_stripped():
    from outreach.providers.social import classify_social_url

    link = classify_social_url("https://instagram.com/sunsalon/?igshid=abc123")
    assert link.url == "https://instagram.com/sunsalon"


def test_a_non_social_url_classifies_as_nothing():
    from outreach.providers.social import classify_social_url

    assert classify_social_url("https://sunsalon.com.my") is None


def test_every_linked_profile_is_collected():
    """A Facebook page usually carries the Instagram in its header."""
    from outreach.providers.social import extract_social_links

    html = """
      <a href="https://www.instagram.com/sunsalonpg/">Instagram</a>
      <a href="https://www.tiktok.com/@sunsalonpg">TikTok</a>
      <a href="https://wa.me/60123456789">WhatsApp</a>
    """
    links = extract_social_links(html, known="https://facebook.com/sunsalonpg")

    assert {link.platform for link in links} == {
        "Facebook", "Instagram", "TikTok", "WhatsApp",
    }


def test_the_known_profile_comes_first():
    """It is the one on record, so it leads the list on the site."""
    from outreach.providers.social import extract_social_links

    links = extract_social_links(
        '<a href="https://instagram.com/sunsalonpg">ig</a>',
        known="https://facebook.com/sunsalonpg",
    )
    assert links[0].platform == "Facebook"


def test_share_buttons_and_login_links_are_not_profiles():
    from outreach.providers.social import extract_social_links

    html = """
      <a href="https://www.facebook.com/sharer/sharer.php?u=x">Share</a>
      <a href="https://www.facebook.com/login">Log in</a>
      <a href="https://twitter.com/intent/tweet?text=x">Tweet</a>
      <a href="https://instagram.com/realsalon">Us</a>
    """
    links = extract_social_links(html)

    assert [link.url for link in links] == ["https://instagram.com/realsalon"]


def test_the_same_profile_linked_twice_appears_once():
    from outreach.providers.social import extract_social_links

    html = (
        '<a href="https://instagram.com/sunsalonpg">top</a>'
        '<a href="https://instagram.com/sunsalonpg/">footer</a>'
    )
    assert len(extract_social_links(html)) == 1


def test_relative_and_javascript_links_are_ignored():
    from outreach.providers.social import extract_social_links

    html = '<a href="/about">a</a><a href="javascript:void(0)">b</a>'
    assert extract_social_links(html) == ()


def test_protocol_relative_links_are_read():
    from outreach.providers.social import extract_social_links

    links = extract_social_links('<a href="//instagram.com/sunsalonpg">ig</a>')
    assert links[0].platform == "Instagram"


def test_links_text_is_one_profile_per_line():
    from outreach.providers.social import SocialLink, SocialProfile

    profile = SocialProfile(
        links=(
            SocialLink("Facebook", "@sun", "https://facebook.com/sun"),
            SocialLink("Instagram", "@sunpg", "https://instagram.com/sunpg"),
        )
    )
    assert profile.links_text == (
        "Facebook @sun https://facebook.com/sun\n"
        "Instagram @sunpg https://instagram.com/sunpg"
    )


def test_a_login_wall_still_records_the_profile_we_started_from(monkeypatch):
    """The usual outcome, and the profile is the most useful part."""
    from outreach.providers import social as social_provider
    from outreach.providers.web import PageResult

    monkeypatch.setattr(
        social_provider.web,
        "fetch",
        lambda url, **kw: PageResult(url=url, status="login required"),
    )
    profile = social_provider.lookup("https://facebook.com/sunsalonpg")

    assert profile.status == "login required"
    assert profile.links[0].platform == "Facebook"
    assert profile.links[0].url == "https://facebook.com/sunsalonpg"


def test_handle_from_url_still_works_for_the_columns_that_use_it():
    from outreach.providers.social import handle_from_url

    assert handle_from_url("https://facebook.com/sunsalon") == "@sunsalon"
    assert handle_from_url("https://instagram.com/sunsalon/") == "@sunsalon"
    assert handle_from_url("https://sunsalon.com.my") == ""


# --------------------------------------------------- profiles_text (no lookup)


def test_profiles_text_formats_like_links_text():
    from outreach.providers.social import profiles_text

    assert profiles_text("https://facebook.com/sun", "https://instagram.com/sunpg") == (
        "Facebook @sun https://facebook.com/sun\n"
        "Instagram @sunpg https://instagram.com/sunpg"
    )


def test_profiles_text_drops_unclassifiable_urls():
    """A genuine website must not be echoed as if it were a social link."""
    from outreach.providers.social import profiles_text

    assert profiles_text("https://sunsalon.com.my") == ""
    assert profiles_text("") == ""


def test_profiles_text_drops_duplicates():
    from outreach.providers.social import profiles_text

    assert profiles_text(
        "https://instagram.com/sunpg", "https://instagram.com/sunpg/"
    ) == "Instagram @sunpg https://instagram.com/sunpg"


def test_profiles_text_drops_blank_and_whitespace_only_lines():
    """Hand-edited Sheet cells accumulate stray newlines."""
    from outreach.providers.social import profiles_text

    assert profiles_text("\n  \nhttps://facebook.com/sun\n\n   \n") == (
        "Facebook @sun https://facebook.com/sun"
    )


def test_profiles_text_classifies_urls_with_no_scheme():
    from outreach.providers.social import profiles_text

    assert profiles_text("facebook.com/sun") == "Facebook @sun https://facebook.com/sun"


def test_google_business_site_is_recognised_without_a_synthesised_handle():
    from outreach.providers.social import profiles_text

    assert profiles_text("https://espario.business.site") == (
        "Google Business Site https://espario.business.site"
    )


def test_every_configured_social_domain_is_recognised_by_platform_for():
    """The campaign's list and the provider's list must never diverge again.

    This asserts the relationship, not a fixed list of domains -- pinning the
    campaign file's contents here is exactly the trap that let the two lists
    drift apart in the first place.
    """
    from outreach.config import load_config
    from outreach.providers.social import platform_for

    cfg = load_config("config/campaigns/penang-my.yaml", env_file=None)
    for domain in cfg.discovery.social_domains:
        assert platform_for(f"https://x.{domain}"), (
            f"{domain!r} is in discovery.social_domains but "
            "outreach.providers.social.PLATFORMS does not recognise it"
        )
