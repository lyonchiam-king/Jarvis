"""Jurisdiction gate for outreach campaigns.

Discovery of publicly listed business information is permitted essentially
everywhere. *Contacting* those businesses is not uniformly permitted, and the
rules differ sharply by country. This module refuses to run an outreach stage
in a flagged jurisdiction until the operator has explicitly acknowledged the
local rules in the campaign config.

This is a prompt to get advice, not a substitute for it. Nothing here is legal
advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    OK = "ok"           # B2B cold outreach broadly permitted
    CAUTION = "caution"  # permitted with conditions you must meet
    BLOCKED = "blocked"  # prior consent generally required; do not bulk-send


@dataclass(frozen=True)
class Rule:
    regime: str
    severity: Severity
    summary: str
    requirements: tuple[str, ...]


_EEA = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "IS", "LI", "NO",
}

_GDPR_RULE = Rule(
    regime="GDPR + ePrivacy",
    severity=Severity.CAUTION,
    summary=(
        "Business contact data is still personal data when it identifies an "
        "individual. Cold B2B email is usually defensible under legitimate "
        "interest, but the balancing test must be documented before sending."
    ),
    requirements=(
        "Document a Legitimate Interest Assessment before the first send.",
        "Include your identity and postal address in every message.",
        "Provide a working one-click opt-out and honour it within 30 days.",
        "Be able to state, per lead, where the data came from.",
        "Prefer role addresses (info@, hello@) over named individuals.",
    ),
)

RULES: dict[str, Rule] = {
    "MY": Rule(
        regime="PDPA 2010 (Malaysia)",
        severity=Severity.OK,
        summary=(
            "Publicly listed business details (name, trading address, business "
            "phone) fall outside the PDPA's personal-data scope for commercial "
            "B2B contact."
        ),
        requirements=(
            "Use only publicly listed business contact details.",
            "Honour any opt-out request immediately and permanently.",
            "Do not collect or store data about named individuals.",
        ),
    ),
    "SG": Rule(
        regime="PDPA + DNC Registry (Singapore)",
        severity=Severity.CAUTION,
        summary=(
            "Email to business addresses is broadly fine, but marketing calls "
            "and SMS/WhatsApp to Singapore numbers must be screened against "
            "the Do Not Call registry."
        ),
        requirements=(
            "Screen every phone number against the DNC registry before messaging.",
            "Include a clear opt-out in every message.",
        ),
    ),
    "GB": Rule(
        regime="UK GDPR + PECR",
        severity=Severity.CAUTION,
        summary=(
            "Corporate subscribers may be emailed under legitimate interest, "
            "but sole traders and partnerships are treated as individuals and "
            "require the stricter consent standard."
        ),
        requirements=_GDPR_RULE.requirements
        + ("Sole traders and partnerships need consent, not legitimate interest.",),
    ),
    "CA": Rule(
        regime="CASL (Canada)",
        severity=Severity.BLOCKED,
        summary=(
            "CASL requires express or implied consent *before* a commercial "
            "electronic message is sent. Cold outreach without a pre-existing "
            "relationship carries penalties up to CAD 10M."
        ),
        requirements=(
            "Do not bulk-send. Obtain express consent, or rely on a documented "
            "conspicuously-published-address exemption assessed per lead.",
            "Take specific legal advice before running any campaign here.",
        ),
    ),
    "US": Rule(
        regime="CAN-SPAM",
        severity=Severity.OK,
        summary="Cold B2B email is permitted provided the message is honest and opt-out works.",
        requirements=(
            "No deceptive subject lines or headers.",
            "Include a valid physical postal address.",
            "Honour opt-outs within 10 business days.",
        ),
    ),
    "AU": Rule(
        regime="Spam Act 2003 (Australia)",
        severity=Severity.CAUTION,
        summary=(
            "Relies on inferred consent from a conspicuously published business "
            "address, and only where the message relates to that person's role."
        ),
        requirements=(
            "Only contact addresses published without a 'no unsolicited email' notice.",
            "Message must be relevant to the recipient's business function.",
            "Include sender identification and a functional unsubscribe.",
        ),
    ),
    "ID": Rule(
        regime="PDP Law (Indonesia)",
        severity=Severity.CAUTION,
        summary="Newer regime with an evolving enforcement posture; treat business data conservatively.",
        requirements=(
            "Restrict to publicly listed business contact points.",
            "Maintain a suppression list and honour opt-outs immediately.",
        ),
    ),
    "TH": Rule(
        regime="PDPA (Thailand)",
        severity=Severity.CAUTION,
        summary="GDPR-influenced regime; legitimate interest exists but must be documented.",
        requirements=(
            "Document a lawful basis before the first send.",
            "Provide clear opt-out and honour it.",
        ),
    ),
}

for _code in _EEA:
    RULES.setdefault(_code, _GDPR_RULE)


_UNKNOWN = Rule(
    regime="Unknown jurisdiction",
    severity=Severity.CAUTION,
    summary=(
        "No rule is on file for this country code. Assume the strictest "
        "interpretation until you have checked local marketing law."
    ),
    requirements=(
        "Confirm whether prior consent is required for commercial messages.",
        "Add a rule for this country to compliance.py once confirmed.",
    ),
)


class ComplianceBlocked(Exception):
    """Raised when a campaign must not proceed in its configured country."""


def rule_for(country_code: str) -> Rule:
    return RULES.get((country_code or "").upper(), _UNKNOWN)


def format_notice(country_code: str) -> str:
    rule = rule_for(country_code)
    lines = [
        f"Jurisdiction: {country_code.upper()} -- {rule.regime} [{rule.severity.value.upper()}]",
        rule.summary,
        "Requirements:",
    ]
    lines.extend(f"  - {r}" for r in rule.requirements)
    return "\n".join(lines)


def check_outreach_allowed(country_code: str, acknowledged: bool) -> Rule:
    """Gate an outreach stage. Discovery does not call this.

    OK jurisdictions pass silently. CAUTION requires ``acknowledged: true`` in
    the campaign config. BLOCKED refuses regardless -- changing that is a
    deliberate code edit, not a config toggle.
    """
    rule = rule_for(country_code)

    if rule.severity is Severity.BLOCKED:
        raise ComplianceBlocked(
            f"Outreach is blocked for {country_code.upper()}.\n\n"
            f"{format_notice(country_code)}\n\n"
            "This gate is intentionally not config-overridable."
        )

    if rule.severity is Severity.CAUTION and not acknowledged:
        raise ComplianceBlocked(
            f"{country_code.upper()} is a flagged jurisdiction and the campaign "
            "has not acknowledged the local rules.\n\n"
            f"{format_notice(country_code)}\n\n"
            "Once you have met the requirements above, set "
            "`compliance.acknowledged: true` in your campaign YAML."
        )

    return rule
