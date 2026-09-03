"""Reaching people outside Malaysia, and knowing before the site is built.

Two bugs are pinned here. The first is that every phone rule in this project
was Malaysian: the day the campaign moved to New Orleans, every US lead came
back "landline" and the queue went dark. The second is ordering -- the
pipeline used to learn a lead was uncontactable only after it had paid to
build them a website.
"""

import pytest

from outreach import phone
from outreach.providers.whatsapp import is_mobile_number
from outreach.stages.reachability import (
    can_whatsapp,
    is_contactable,
    is_waiting_on_a_check,
)


class TestCountriesOtherThanMalaysia:
    @pytest.mark.parametrize("number", ["+1 504-555-0123", "(504) 555-0123", "+1 212 555 0147"])
    def test_a_us_number_is_unknown_not_landline(self, number):
        """The NANP assigns no mobile range, so no offline rule can answer.
        Calling it a landline is what emptied the US queue -- 'unknown' is
        the honest answer and the one that sends it for a real check."""
        assert phone.line_type(number, "US") == "unknown"

    @pytest.mark.parametrize(
        "number,country,expected",
        [
            ("+60 12-3456789", "MY", "mobile"),
            ("04-262 1188", "MY", "landline"),
            ("+44 7700 900123", "GB", "mobile"),
            ("020 7946 0958", "GB", "landline"),
            ("+61 412 345 678", "AU", "mobile"),
        ],
    )
    def test_countries_with_a_real_rule_still_answer(self, number, country, expected):
        assert phone.line_type(number, country) == expected

    def test_a_country_we_have_no_rule_for_is_unknown_not_wrong(self):
        """Better to check than to guess. An unlisted country must not
        silently resolve to 'landline' and retire every lead in it."""
        assert phone.line_type("+81 90 1234 5678", "JP") == "unknown"

    def test_the_country_is_read_off_the_number_when_the_cell_is_blank(self):
        """Rows discovered before the Country column was filled in still hold
        an international number, and it names its own country."""
        assert phone.line_type("60123456789", "") == "mobile"
        assert phone.line_type("6042621188", "") == "landline"

    def test_a_national_number_alone_names_no_country(self):
        """012-3456789 is Malaysian, British or neither depending on where
        you are standing. Guessing here is how a message reaches a stranger."""
        assert phone.country_from_number("012-3456789") == ""


class TestNormalising:
    @pytest.mark.parametrize(
        "number,country,expected",
        [
            ("+1 504-555-0123", "US", "15045550123"),
            ("(504) 555-0123", "US", "15045550123"),
            ("012-3456789", "MY", "60123456789"),
            ("+60 12-3456789", "MY", "60123456789"),
        ],
    )
    def test_numbers_reach_e164(self, number, country, expected):
        assert phone.normalise(number, country) == expected

    def test_a_blank_number_stays_blank(self):
        """Callers must treat "" as unreachable, never as something to try."""
        assert phone.normalise("", "US") == ""
        assert phone.normalise("   ", "MY") == ""

    def test_no_country_code_is_ever_invented(self):
        """The relay used to force a 60 onto anything without one, so the US
        number 15045550123 became 6015045550123 -- a real Malaysian line
        belonging to a stranger. Nothing here may add a code it was not given."""
        assert phone.normalise("15045550123", "") == "15045550123"


class TestTheCachedVerdictWins:
    def test_a_confirmed_us_number_is_reachable_despite_the_plan(self):
        """The whole point of asking WhatsApp: the plan says unknown, the
        lookup says yes, and the lookup is the one that knows."""
        lead = {"Phone": "+1 504-555-0123", "Country": "US"}
        assert can_whatsapp(lead, {"WhatsApp Reachable": "yes"}) is True

    def test_a_confirmed_no_beats_a_mobile_looking_number(self):
        lead = {"Phone": "+60 12-3456789", "Country": "MY"}
        assert can_whatsapp(lead, {"WhatsApp Reachable": "no"}) is False

    def test_an_unchecked_lead_is_not_assumed_reachable(self):
        """There used to be a numbering-plan fallback here. It read a UK
        landline as unreachable and a mobile as reachable, and both are
        guesses -- so it decided a lead's fate without asking anyone. An
        unchecked lead is simply not confirmed, and the build gate holds it."""
        lead = {"Phone": "+60 12-3456789", "Country": "MY"}
        assert can_whatsapp(lead, {}) is False

    def test_a_landline_confirmed_on_whatsapp_is_reachable(self):
        """WhatsApp Business verifies fixed lines by voice call. Whatever the
        number looks like, a yes from WhatsApp is a yes."""
        lead = {"Phone": "+44 20 7946 0958", "Country": "GB"}
        assert can_whatsapp(lead, {"WhatsApp Reachable": "yes"}) is True


class TestTheGateBeforeBuilding:
    def test_no_channel_at_all_is_the_only_thing_that_is_dropped(self):
        assert is_contactable({"WhatsApp Reachable": "no", "Email Found": ""}) is False

    def test_an_email_keeps_a_landline_lead_alive(self):
        """Lyon's rule. Plenty of US businesses list a landline and an email
        and are perfectly contactable that way."""
        row = {"WhatsApp Reachable": "no", "Email Found": "hi@shop.com"}
        assert is_contactable(row) is True

    def test_not_found_is_not_an_email_address(self):
        row = {"WhatsApp Reachable": "no", "Email Found": "not found"}
        assert is_contactable(row) is False

    def test_an_unchecked_lead_is_held_rather_than_built_for(self):
        """This rule was the wrong way round at first.

        The original reasoning was that a lookup which has not run is not
        evidence, so an unchecked lead should pass. True about evidence,
        wrong about what to do with none: the safe move is to wait, not to
        spend. Five US leads went all the way to a finished site and only
        then turned out to be unmessageable, which is the exact cost this
        gate exists to avoid.
        """
        assert is_contactable({}) is False
        assert is_contactable({"WhatsApp Reachable": "unknown"}) is False

    def test_a_held_lead_is_reported_as_waiting_not_as_unreachable(self):
        """Held and rejected must not look alike: one needs the relay
        connecting, the other is a genuine dead end. Collapsing them is how
        someone concludes their leads vanished."""
        assert is_waiting_on_a_check({}) is True
        assert is_waiting_on_a_check({"WhatsApp Reachable": "no"}) is False

    def test_an_email_releases_a_lead_the_check_never_answered_for(self):
        """An email is a confirmed channel on its own, so it does not wait
        on WhatsApp -- otherwise an offline relay would stall leads that
        were always contactable."""
        assert is_contactable({"Email Found": "hi@shop.com"}) is True
        assert is_waiting_on_a_check({"Email Found": "hi@shop.com"}) is False


def test_is_mobile_number_still_answers_for_malaysia():
    """The old callers' behaviour, preserved now that it takes a country."""
    assert is_mobile_number("+60 12-3456789", "MY") is True
    assert is_mobile_number("04-262 1188", "MY") is False


class TestTheRelayClientIsActuallyBuilt:
    """The bug that made all of the above pointless.

    `_default_client` read a nested `outreach.whatsapp` object. The real
    config is flat -- `outreach.whatsapp_enabled` -- so getattr found
    nothing and returned None on a campaign where WhatsApp was switched on.
    No lookup ever ran, no verdict was ever written, and five US leads were
    researched, briefed and BUILT before anyone found out they could not be
    messaged. Defensive getattr is what hid it: it turned a typo into a
    silent no-op.
    """

    def _config(self, enabled=True):
        from dataclasses import replace
        from outreach.config import load_config

        cfg = load_config("config/campaigns/penang-my.yaml", env_file=None)
        return replace(
            cfg, outreach=replace(cfg.outreach, whatsapp_enabled=enabled)
        )

    def test_an_enabled_campaign_gets_a_real_client(self):
        from outreach.stages.reachability import _default_client

        client = _default_client(self._config(enabled=True))
        assert client is not None, (
            "WhatsApp is enabled, so a client must be built -- returning None "
            "here silently disables every lookup"
        )

    def test_the_client_uses_the_configured_relay_port(self):
        from dataclasses import replace
        from outreach.stages.reachability import _default_client

        cfg = self._config(enabled=True)
        cfg = replace(cfg, outreach=replace(cfg.outreach, whatsapp_relay_port=4111))
        assert "4111" in _default_client(cfg)._base

    def test_a_disabled_campaign_gets_no_client(self):
        from outreach.stages.reachability import _default_client

        assert _default_client(self._config(enabled=False)) is None


class TestLegacyRowsHealThemselves:
    """Rows written before the country came from Google.

    Those carry a national phone number and a Country cell stamped from
    `compliance.country_code`. On a UK campaign derived from the Penang
    config that cell says MY, so 07450 225230 was dialled as 607450225230 --
    a real Malaysian line, and the reason a send could report success and
    reach nobody. The address is the one field on those rows still telling
    the truth, so it is read before the stored country.
    """

    def _lead(self, **over):
        base = {
            "Phone": "07450 225230",
            "Country": "MY",
            "Address": "12 High St, Manchester, UK",
        }
        base.update(over)
        return base

    def test_the_address_overrides_a_wrong_country_cell(self):
        from outreach.stages.reachability import _country_of

        assert _country_of(self._lead(), "MY") == "GB"

    def test_such_a_lead_now_dials_correctly_without_a_refresh(self):
        from outreach.stages.reachability import _country_of

        lead = self._lead()
        dialled = phone.normalise(lead["Phone"], _country_of(lead, "MY"))

        assert dialled == "447450225230"
        assert not dialled.startswith("60")

    def test_a_genuine_malaysian_row_is_untouched(self):
        """The rescue must not disturb the market that always worked."""
        from outreach.stages.reachability import _country_of

        lead = self._lead(
            Phone="012-345 6789", Address="5 Lebuh Chulia, George Town, Malaysia"
        )
        assert _country_of(lead, "MY") == "MY"
        assert phone.normalise(lead["Phone"], _country_of(lead, "MY")) == "60123456789"

    def test_the_number_s_own_prefix_still_wins(self):
        """An international number cannot be wrong, so nothing overrides it."""
        from outreach.stages.reachability import _country_of

        lead = self._lead(Phone="+44 7450 225230", Address="Nowhere in particular")
        assert _country_of(lead, "MY") == "GB"

    def test_an_unrecognised_address_falls_back_rather_than_guessing(self):
        from outreach.stages.reachability import _country_of

        lead = self._lead(Country="", Address="somewhere odd")
        assert _country_of(lead, "MY") == "MY"
