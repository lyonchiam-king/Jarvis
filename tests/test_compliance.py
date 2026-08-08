import pytest

from outreach.compliance import (
    ComplianceBlocked,
    Severity,
    check_outreach_allowed,
    format_notice,
    rule_for,
)


def test_malaysia_passes_without_acknowledgement():
    rule = check_outreach_allowed("MY", acknowledged=False)
    assert rule.severity is Severity.OK


def test_uk_requires_acknowledgement():
    with pytest.raises(ComplianceBlocked, match="flagged jurisdiction"):
        check_outreach_allowed("GB", acknowledged=False)
    assert check_outreach_allowed("GB", acknowledged=True).severity is Severity.CAUTION


def test_eea_member_inherits_gdpr_rule():
    assert rule_for("DE").regime == "GDPR + ePrivacy"
    assert rule_for("FR").severity is Severity.CAUTION


def test_canada_is_blocked_and_not_config_overridable():
    with pytest.raises(ComplianceBlocked, match="not config-overridable"):
        check_outreach_allowed("CA", acknowledged=True)


def test_unknown_country_defaults_to_caution_not_permitted():
    rule = rule_for("ZZ")
    assert rule.severity is Severity.CAUTION
    with pytest.raises(ComplianceBlocked):
        check_outreach_allowed("ZZ", acknowledged=False)


def test_country_code_is_case_insensitive():
    assert rule_for("my").regime.startswith("PDPA")


def test_notice_lists_actionable_requirements():
    notice = format_notice("GB")
    assert "UK GDPR" in notice
    assert "Legitimate Interest Assessment" in notice
