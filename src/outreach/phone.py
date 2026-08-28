"""Phone numbers across countries, and whether WhatsApp can reach one.

Jarvis was written for Penang, so the original test was one line: a Malaysian
number starting 01 is a mobile, anything else is a landline. That is true in
Malaysia and false almost everywhere else. The moment the campaign moved to
New Orleans every lead came back "landline" -- US numbers carry no digit that
separates mobile from fixed line -- and the whole US queue went dark.

So this module answers a smaller question honestly rather than a big one
badly. `line_type` returns `unknown` whenever the country's numbering plan
genuinely cannot tell, and `unknown` means *ask WhatsApp*, never *give up*.
The lookup that resolves it lives in providers/whatsapp.py; this file stays
offline and has no opinion about the network.
"""

from __future__ import annotations

import re

# Dialling codes for the countries a campaign is plausibly aimed at, keyed by
# the ISO code Google Places returns in the lead's Country column. Adding a
# country is a line here plus, optionally, a rule in _MOBILE_RULES -- a
# country absent from both still works, it just resolves to `unknown` and
# gets checked against WhatsApp rather than guessed at.
DIALLING_CODES: dict[str, str] = {
    # Every ITU calling code, so no campaign can land on a country the tool
    # cannot dial. A missing entry does not fail loudly -- it leaves a
    # national number un-normalised, which reads downstream as "not on
    # WhatsApp", so the whole table is here rather than the handful of
    # countries that happened to come up.
    "AD": "376", "AE": "971", "AF": "93", "AG": "1", "AI": "1", "AL": "355",
    "AM": "374", "AO": "244", "AR": "54", "AS": "1", "AT": "43", "AU": "61",
    "AW": "297", "AZ": "994", "BA": "387", "BB": "1", "BD": "880", "BE": "32",
    "BF": "226", "BG": "359", "BH": "973", "BI": "257", "BJ": "229", "BM": "1",
    "BN": "673", "BO": "591", "BR": "55", "BS": "1", "BT": "975", "BW": "267",
    "BY": "375", "BZ": "501", "CA": "1", "CD": "243", "CF": "236", "CG": "242",
    "CH": "41", "CI": "225", "CL": "56", "CM": "237", "CN": "86", "CO": "57",
    "CR": "506", "CU": "53", "CV": "238", "CW": "599", "CY": "357", "CZ": "420",
    "DE": "49", "DJ": "253", "DK": "45", "DM": "1", "DO": "1", "DZ": "213",
    "EC": "593", "EE": "372", "EG": "20", "ER": "291", "ES": "34", "ET": "251",
    "FI": "358", "FJ": "679", "FM": "691", "FO": "298", "FR": "33", "GA": "241",
    "GB": "44", "GD": "1", "GE": "995", "GF": "594", "GH": "233", "GI": "350",
    "GL": "299", "GM": "220", "GN": "224", "GP": "590", "GQ": "240", "GR": "30",
    "GT": "502", "GU": "1", "GW": "245", "GY": "592", "HK": "852", "HN": "504",
    "HR": "385", "HT": "509", "HU": "36", "ID": "62", "IE": "353", "IL": "972",
    "IM": "44", "IN": "91", "IQ": "964", "IR": "98", "IS": "354", "IT": "39",
    "JE": "44", "JM": "1", "JO": "962", "JP": "81", "KE": "254", "KG": "996",
    "KH": "855", "KI": "686", "KM": "269", "KN": "1", "KP": "850", "KR": "82",
    "KW": "965", "KY": "1", "KZ": "7", "LA": "856", "LB": "961", "LC": "1",
    "LI": "423", "LK": "94", "LR": "231", "LS": "266", "LT": "370", "LU": "352",
    "LV": "371", "LY": "218", "MA": "212", "MC": "377", "MD": "373", "ME": "382",
    "MG": "261", "MH": "692", "MK": "389", "ML": "223", "MM": "95", "MN": "976",
    "MO": "853", "MP": "1", "MQ": "596", "MR": "222", "MS": "1", "MT": "356",
    "MU": "230", "MV": "960", "MW": "265", "MX": "52", "MY": "60", "MZ": "258",
    "NA": "264", "NC": "687", "NE": "227", "NG": "234", "NI": "505", "NL": "31",
    "NO": "47", "NP": "977", "NR": "674", "NZ": "64", "OM": "968", "PA": "507",
    "PE": "51", "PF": "689", "PG": "675", "PH": "63", "PK": "92", "PL": "48",
    "PR": "1", "PS": "970", "PT": "351", "PW": "680", "PY": "595", "QA": "974",
    "RE": "262", "RO": "40", "RS": "381", "RU": "7", "RW": "250", "SA": "966",
    "SB": "677", "SC": "248", "SD": "249", "SE": "46", "SG": "65", "SI": "386",
    "SK": "421", "SL": "232", "SM": "378", "SN": "221", "SO": "252", "SR": "597",
    "SS": "211", "SV": "503", "SY": "963", "SZ": "268", "TC": "1", "TD": "235",
    "TG": "228", "TH": "66", "TJ": "992", "TL": "670", "TM": "993", "TN": "216",
    "TO": "676", "TR": "90", "TT": "1", "TV": "688", "TW": "886", "TZ": "255",
    "UA": "380", "UG": "256", "UK": "44", "US": "1", "UY": "598", "UZ": "998",
    "VC": "1", "VE": "58", "VG": "1", "VI": "1", "VN": "84", "VU": "678",
    "WS": "685", "YE": "967", "ZA": "27", "ZM": "260", "ZW": "263",
}


# National-format prefixes that mean "mobile" in a given country, checked
# after the trunk zero is restored. Only countries whose plan genuinely
# separates the two appear here. The US and Canada deliberately do not: the
# NANP assigns no mobile range, which is the entire reason this module
# exists.
_MOBILE_RULES: dict[str, tuple[str, ...]] = {
    "MY": ("01",),
    "GB": ("07",),
    "UK": ("07",),
    "AU": ("04",),
    "NZ": ("02",),
    "IE": ("08",),
    "ZA": ("06", "07", "08"),
    "IN": ("06", "07", "08", "09"),
    "ID": ("08",),
    "PH": ("09",),
    "VN": ("03", "05", "07", "08", "09"),
    "TH": ("06", "08", "09"),
}

# Countries where every mobile and landline shares one format, so no offline
# rule can ever separate them. Named explicitly so the reason shows up in a
# report rather than looking like a country we forgot.
AMBIGUOUS_PLANS = frozenset({"US", "CA"})


def digits_only(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def normalise(phone: str, country: str = "") -> str:
    """To E.164 without the plus -- the form WhatsApp wants for a JID.

    Returns "" when there is nothing usable, which callers must treat as
    "cannot reach", never as a number to try anyway.
    """
    digits = digits_only(phone)
    if not digits:
        return ""

    code = DIALLING_CODES.get((country or "").strip().upper(), "")

    # Deliberately NOT "does this look international already?". A US
    # national number 5045550123 opens with 504, which is Honduras' code, so
    # guessing from the digits alone silently dropped the US country code.
    # Idempotency is the caller's job instead: pass country="" for a number
    # that is already E.164 and it comes back untouched (see below).
    if code and digits.startswith(code):
        rest = digits[len(code):]
        # A leading zero after the country code is a trunk prefix that got
        # left in ("+60 012..."); it is never part of the subscriber number.
        return code + rest.lstrip("0") if rest.startswith("0") else digits

    # National format with a trunk zero: 012-345 6789, (504) 555-0123.
    if code:
        return code + digits.lstrip("0")

    # No country on the lead. Assume it is already international rather than
    # inventing a code -- a guessed country code sends a real message to a
    # stranger in another country.
    return digits


def national(phone: str, country: str = "") -> str:
    """The subscriber part with its trunk zero restored, for prefix rules."""
    e164 = normalise(phone, country)
    if not e164:
        return ""
    code = DIALLING_CODES.get((country or "").strip().upper(), "")
    if code and e164.startswith(code):
        return "0" + e164[len(code):]
    return e164


# Longest-first, so 353 (Ireland) is matched before 35, and 1 last of all.
_CODE_TO_COUNTRY = sorted(
    ((code, iso) for iso, code in DIALLING_CODES.items() if iso not in {"UK", "CA"}),
    key=lambda pair: -len(pair[0]),
)


def country_from_number(phone: str) -> str:
    """Guess the ISO country from an international number's own prefix.

    Google Places returns numbers in international format most of the time,
    so the number usually carries its own country even when the lead's
    Country cell is blank -- which is the case for every row discovered
    before that column was filled in. Returns "" when nothing matches, and
    the caller then has no country rather than a wrong one.
    """
    digits = digits_only(phone)
    if not digits:
        return ""
    for code, iso in _CODE_TO_COUNTRY:
        if digits.startswith(code):
            # A national number that merely happens to start with the digit
            # is not an international one: 012... is not +1 2...
            if digits.startswith("0"):
                return ""
            return iso
    return ""


def resolve_country(phone: str, country: str = "") -> str:
    """The lead's stated country, or the one its number implies."""
    stated = (country or "").strip().upper()
    return stated or country_from_number(phone)


def line_type(phone: str, country: str = "") -> str:
    """"mobile", "landline" or "unknown", according to the numbering plan.

    Informational only. This must NEVER be used to decide whether WhatsApp
    can reach someone, in either direction:

      * a landline is not a no -- WhatsApp Business verifies fixed lines by
        voice call, and a great many small shops run it on the number over
        the door. Reading "landline" as "unreachable" marked every UK
        business with an 020 or 0161 number as uncontactable without ever
        asking, which is exactly what it looked like: none of them had
        WhatsApp, and by hand, plenty of them did.
      * a mobile is not a yes -- plenty of mobiles have no WhatsApp account.

    Only WhatsApp knows. `providers.whatsapp.check_numbers` asks it.
    """
    if not digits_only(phone):
        return "unknown"

    key = resolve_country(phone, country)
    if key in AMBIGUOUS_PLANS:
        return "unknown"

    rules = _MOBILE_RULES.get(key)
    if not rules:
        return "unknown"

    local = national(phone, key)
    return "mobile" if local.startswith(rules) else "landline"


def whatsapp_jid(phone: str, country: str = "") -> str:
    e164 = normalise(phone, country)
    return f"{e164}@s.whatsapp.net" if e164 else ""
