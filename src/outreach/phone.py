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
    # Asia-Pacific
    "MY": "60", "SG": "65", "ID": "62", "TH": "66", "PH": "63", "VN": "84",
    "IN": "91", "HK": "852", "TW": "886", "JP": "81", "KR": "82", "CN": "86",
    "AU": "61", "NZ": "64",
    # Americas
    "US": "1", "CA": "1", "MX": "52", "BR": "55", "AR": "54", "CL": "56",
    # British Isles
    "GB": "44", "UK": "44", "IE": "353",
    # Western Europe
    "DE": "49", "FR": "33", "ES": "34", "IT": "39", "PT": "351", "NL": "31",
    "BE": "32", "LU": "352", "AT": "43", "CH": "41",
    # Nordics
    "DK": "45", "SE": "46", "NO": "47", "FI": "358", "IS": "354",
    # Central and Eastern Europe
    "PL": "48", "CZ": "420", "SK": "421", "HU": "36", "RO": "40", "BG": "359",
    "GR": "30", "HR": "385", "SI": "386", "RS": "381", "EE": "372",
    "LV": "371", "LT": "370", "CY": "357", "MT": "356",
    # Middle East and Africa
    "AE": "971", "SA": "966", "QA": "974", "IL": "972", "TR": "90",
    "ZA": "27", "NG": "234", "KE": "254", "EG": "20",
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

    # Already international: Places usually returns +60 12-345 6789 or
    # +1 504-555-0123, so the country code is normally already on the front.
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
