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


class TestAnyCountryWorksWithoutACodeChange:
    """Item 1: scrape a new country tomorrow and it must simply work.

    Every country-specific failure in this project came from a rule that
    named one country. There are none left on the reachability path: the
    dialling table covers every ITU code, and the country comes from Google
    per lead, so adding a market is a config change and nothing more.
    """

    SAMPLE = [
        ("GB", "+44 20 7946 0958", "442079460958"),
        ("US", "+1 504-555-0123", "15045550123"),
        ("DE", "+49 30 12345678", "493012345678"),
        ("FR", "+33 1 42 68 53 00", "33142685300"),
        ("ES", "+34 91 123 4567", "34911234567"),
        ("IT", "+39 06 1234 5678", "390612345678"),
        ("NL", "+31 20 123 4567", "31201234567"),
        ("PL", "+48 22 123 4567", "48221234567"),
        ("SE", "+46 8 123 456", "468123456"),
        ("JP", "+81 90 1234 5678", "819012345678"),
        ("BR", "+55 11 5555-1234", "551155551234"),
        ("ZA", "+27 82 123 4567", "27821234567"),
        ("AE", "+971 4 123 4567", "97141234567"),
        ("MY", "+60 12-345 6789", "60123456789"),
    ]

    def test_every_sampled_country_dials_correctly(self):
        for iso, intl, expected in self.SAMPLE:
            raw = {
                "id": "p",
                "displayName": {"text": "X"},
                "formattedAddress": "somewhere",
                "addressComponents": [{"shortText": iso, "types": ["country"]}],
                "internationalPhoneNumber": intl,
            }
            place = _parse_place(raw)
            assert place.country == iso
            # country="" -- the send path's contract for an already-E.164
            # number, and the one that must never re-prefix.
            assert phone.normalise(place.phone, "") == expected, iso

    def test_the_dialling_table_covers_every_sampled_country(self):
        for iso, _intl, _expected in self.SAMPLE:
            assert iso in phone.DIALLING_CODES, f"{iso} cannot be dialled"

    def test_no_country_is_privileged_in_the_reachability_path(self):
        """Malaysia must have no special standing left anywhere."""
        import inspect
        from outreach.stages import reachability

        src = inspect.getsource(reachability)
        for token in ('"MY"', "'MY'", '"60"', "'60'"):
            assert token not in src, f"{token} hardcodes a country into the check"
