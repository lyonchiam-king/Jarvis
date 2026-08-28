"""Where a lead's country actually comes from.

Lyon's UK number 07450 225230 was on WhatsApp when typed by hand and
"not reachable" from the pipeline. The lookup was fine by then. The fault
was upstream: Google returns both a national and an international phone
number, we stored the national one, and then rebuilt the country from the
campaign's `compliance.country_code` -- which on a campaign copied from
Penang still said MY. So the UK number was dialled as 607450225230.
"""

from outreach import phone
from outreach.providers.places import _parse_details, _parse_place

RAW = {
    "id": "place-1",
    "displayName": {"text": "Corner Barbers"},
    "formattedAddress": "12 High St, Manchester, UK",
    "nationalPhoneNumber": "07450 225230",
    "internationalPhoneNumber": "+44 7450 225230",
}


class TestThePhoneWeKeep:
    def test_search_keeps_the_international_number(self):
        """The national form names no country, so keeping it throws away the
        one piece of information that makes the number dialable abroad."""
        assert _parse_place(RAW).phone == "+44 7450 225230"

    def test_details_keeps_the_international_number(self):
        assert _parse_details(RAW).phone == "+44 7450 225230"

    def test_the_national_number_is_still_used_when_it_is_all_there_is(self):
        """Some listings carry only the national form. Better a number that
        needs a country than no number at all."""
        raw = {k: v for k, v in RAW.items() if k != "internationalPhoneNumber"}
        assert _parse_place(raw).phone == "07450 225230"


class TestTheNumberSurvivesToWhatsApp:
    def test_lyons_actual_number_dials_correctly(self):
        """The exact number from the bug report, end to end."""
        stored = _parse_place(RAW).phone
        country = phone.country_from_number(stored)

        assert country == "GB"
        assert phone.normalise(stored, country) == "447450225230"

    def test_the_campaign_country_can_no_longer_corrupt_it(self):
        """Even with MY configured -- the state Lyon's UK campaign was in --
        an international number is dialled by its own country code."""
        stored = _parse_place(RAW).phone
        resolved = phone.resolve_country(stored, "")

        assert phone.normalise(stored, resolved) == "447450225230"
        assert not phone.normalise(stored, resolved).startswith("60")

    def test_a_national_number_with_the_wrong_campaign_country_is_the_old_bug(self):
        """Kept as the worked example of what went wrong, so nobody
        reintroduces the national-first preference: without a country code on
        the number, a wrong campaign setting silently dials another country."""
        assert phone.normalise("07450 225230", "MY") == "607450225230"
        assert phone.normalise("07450 225230", "GB") == "447450225230"
