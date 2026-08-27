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
from outreach.stages.reachability import can_whatsapp, is_contactable


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

    def test_an_unchecked_lead_falls_back_to_the_plan(self):
        lead = {"Phone": "+60 12-3456789", "Country": "MY"}
        assert can_whatsapp(lead, {}) is True

    def test_the_campaign_country_rescues_a_bare_national_number(self):
        """Existing Penang rows hold '012-3456789' with no Country cell. If
        that resolved to unreachable, the whole live sheet would go dark."""
        lead = {"Phone": "012-3456789", "Country": ""}
        assert can_whatsapp(lead, {}, "MY") is True


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

    def test_an_unchecked_lead_is_never_dropped(self):
        """A lookup that has not run is not evidence. Dropping on it would
        silently discard the entire queue the first time the relay was down."""
        assert is_contactable({}) is True
        assert is_contactable({"WhatsApp Reachable": "unknown"}) is True


def test_is_mobile_number_still_answers_for_malaysia():
    """The old callers' behaviour, preserved now that it takes a country."""
    assert is_mobile_number("+60 12-3456789", "MY") is True
    assert is_mobile_number("04-262 1188", "MY") is False
