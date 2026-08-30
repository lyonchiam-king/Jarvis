"""Stage 5, plus the source-level proof that this codebase cannot send email."""

import ast
import base64
from dataclasses import replace
from pathlib import Path

import pytest

from outreach import schema
from outreach.compliance import ComplianceBlocked
from outreach.config import load_config
from outreach.providers import gmail as gmail_provider
from outreach.providers.gmail import GmailClient, GmailError
from outreach.providers.llm import OutreachCopy
from outreach.stages import outreach_draft

SRC = Path(__file__).resolve().parent.parent / "src"


# ------------------------------------------- the guarantee, checked in source


def test_no_send_call_exists_anywhere_in_the_source():
    """The 'never auto-send' promise is code, not an OAuth scope.

    Google publishes no scope that allows drafting while forbidding sending --
    gmail.compose permits both. So the safety property is the absence of a send
    call, and this test is what keeps it absent.

    Parses the AST rather than grepping text, so documentation *about* not
    sending doesn't trip it and a real ``.send(...)`` cannot hide behind
    formatting.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            # Any attribute named `send` -- called, aliased, or referenced.
            if isinstance(node, ast.Attribute) and node.attr == "send":
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: .send")
            # getattr(obj, "send") sneaking past the check above.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "send"
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: getattr send")

    assert not offenders, "Found something that could send mail:\n" + "\n".join(offenders)


def test_gmail_scope_is_compose_not_send():
    from outreach.auth import SCOPES

    assert "https://www.googleapis.com/auth/gmail.compose" in SCOPES
    assert not any(s.endswith("gmail.send") for s in SCOPES)
    assert not any(s.endswith("gmail.modify") for s in SCOPES)


# ------------------------------------------------------------------- gmail


class FakeDrafts:
    def __init__(self, result=None, error=None):
        self.result = result or {"id": "d1", "message": {"id": "m1"}}
        self.error = error
        self.bodies = []

    def create(self, userId, body):  # noqa: N803 -- mirrors the Google client
        self.bodies.append(body)
        error = self.error

        class _Exec:
            def execute(_self):
                if error:
                    raise error
                return self.result

        return _Exec()


class FakeGmailService:
    def __init__(self, drafts=None):
        self._drafts = drafts or FakeDrafts()

    def users(self):
        return self

    def drafts(self):
        return self._drafts


def test_create_draft_encodes_a_valid_rfc822_message():
    drafts = FakeDrafts()
    client = GmailClient(None, service=FakeGmailService(drafts))

    draft = client.create_draft(
        to="owner@salon.my", subject="A thought about your bookings", body="Hi there"
    )

    raw = drafts.bodies[0]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode()
    assert "To: owner@salon.my" in decoded
    assert "Subject: A thought about your bookings" in decoded
    assert "Hi there" in decoded
    assert draft.draft_id == "d1"
    assert "m1" in draft.link


def test_create_draft_refuses_an_invalid_recipient():
    client = GmailClient(None, service=FakeGmailService())
    with pytest.raises(GmailError, match="invalid recipient"):
        client.create_draft(to="not-an-email", subject="x", body="y")


def test_create_draft_refuses_an_empty_subject():
    client = GmailClient(None, service=FakeGmailService())
    with pytest.raises(GmailError, match="empty subject"):
        client.create_draft(to="a@b.com", subject="   ", body="y")


def test_403_explains_the_likely_causes():
    from googleapiclient.errors import HttpError

    class Resp:
        status = 403
        reason = "Forbidden"

    error = HttpError(Resp(), b"denied")
    client = GmailClient(None, service=FakeGmailService(FakeDrafts(error=error)))

    with pytest.raises(GmailError, match="gmail.compose"):
        client.create_draft(to="a@b.com", subject="s", body="b")


def test_footer_carries_the_postal_address_and_opt_out():
    body = gmail_provider.compose_body(
        body="Short note about your bookings.",
        sender_name="Lyon Chiam",
        sender_title="Founder",
        company="COAI",
        postal_address="12 Some Road, 10450 Penang",
        opt_out_note="Reply 'no thanks' and I won't contact you.",
    )
    assert "12 Some Road" in body
    assert "no thanks" in body
    assert body.index("Short note") < body.index("Lyon Chiam") < body.index("Some Road")


# ------------------------------------------------------------------ config


PENANG = "config/campaigns/penang-my.yaml"


def config(**overrides):
    cfg = load_config(PENANG, env_file=None)
    if overrides:
        cfg = replace(cfg, outreach=replace(cfg.outreach, **overrides))
    return cfg


def test_preflight_requires_a_postal_address():
    with pytest.raises(outreach_draft.MissingPostalAddress, match="postal_address"):
        outreach_draft.preflight(config(postal_address=""))


def test_preflight_passes_once_the_address_is_set():
    outreach_draft.preflight(config(postal_address="12 Some Road, Penang"))


def test_preflight_blocks_an_unacknowledged_flagged_jurisdiction():
    cfg = load_config("config/campaigns/example-manchester-uk.yaml", env_file=None)
    cfg = replace(cfg, outreach=replace(cfg.outreach, postal_address="1 Some St"))
    with pytest.raises(ComplianceBlocked, match="flagged jurisdiction"):
        outreach_draft.preflight(cfg)


# --------------------------------------------------------------------- run


def lead(lead_id, stage="Researched", phone="012-3456789"):
    return {
        "Lead ID": lead_id,
        "Stage": stage,
        "Business Name": f"Salon {lead_id}",
        "Category": "Hair salon",
        "Phone": phone,
    }


def research_row(lead_id, email="", whatsapp="yes"):
    """`whatsapp` defaults to "yes": by the time a lead reaches the draft
    stage the check has run, because the build gate holds anything it has
    not answered for. Tests about the no-channel case pass "no"."""
    return {
        "Lead ID": lead_id,
        "WhatsApp Reachable": whatsapp,
        "Business Name": f"Salon {lead_id}",
        "Tone": "warm",
        "Services": "cut, colour",
        "Customer Praise": "Friendly",
        "Customer Pain Points": "Booking is hard",
        "Suggested Website Angle": "Online booking",
        "Email Found": email,
        "Email Source": "social_page" if email else "not found",
    }


def index(rows):
    return {r["Lead ID"]: (i + 2, r) for i, r in enumerate(rows)}


class FakeSheets:
    def __init__(self, leads, research, site_briefs=None, outreach=()):
        self._tabs = {
            schema.LEADS.title: leads,
            schema.RESEARCH.title: research,
            schema.SITE_BRIEFS.title: site_briefs or {},
            schema.OUTREACH_LOG.title: {k: (2, {}) for k in outreach},
        }
        self.upserts = []
        self.column_writes = []
        self.appended = []
        self.spreadsheet_id = "fake"

    def read_rows(self, tab):
        return self._tabs.get(tab.title, {})

    def upsert(self, tab, rows, *, on_conflict=None):
        self.upserts.append((tab, list(rows)))
        from outreach.sheets import UpsertResult

        return UpsertResult(inserted=len(rows))

    def update_column(self, tab, column, values_by_key, *, user_edit=False):
        from outreach.sheets import guard_manual_column

        guard_manual_column(tab, column, user_edit=user_edit)
        self.column_writes.append((tab, column, dict(values_by_key)))
        return len(values_by_key)

    def append(self, tab, rows):
        self.appended.append((tab, list(rows)))
        return len(rows)


class FakeWriter:
    def __init__(self):
        self.call_count = 0
        self.prompts = []

    def write_outreach_copy(self, prompt):
        self.call_count += 1
        self.prompts.append(prompt)
        return OutreachCopy(
            subject="A thought about your bookings",
            email_body="Noticed a few reviews asking how to book.",
            whatsapp_message="Hi! Noticed folks asking how to book online.",
        )


class FakeGmail:
    def __init__(self):
        self.drafted = []

    def create_draft(self, *, to, subject, body):
        self.drafted.append((to, subject, body))
        return gmail_provider.Draft(draft_id="d1", message_id="m1")


def cfg():
    """The no-demo path, pinned rather than inherited.

    ``require_site`` lives in a campaign file the user is expected to edit, so
    a test that depends on it must say so. Only
    ``test_penang_campaign_is_configured_demo_first`` reads that file's value.
    """
    return config(
        postal_address="12 Some Road, 10450 Penang",
        sender_name="Yiu Ming",
        require_site=False,
    )


def test_lead_with_an_email_gets_a_gmail_draft():
    sheets = FakeSheets(index([lead("a")]), index([research_row("a", "o@salon.my")]))
    gmail = FakeGmail()

    report = outreach_draft.run(cfg(), sheets, FakeWriter(), gmail)

    assert report.drafts_created == 1
    assert gmail.drafted[0][0] == "o@salon.my"
    row = sheets.upserts[0][1][0]
    assert row["Channel"] == "Email"
    assert row["Gmail Draft ID"] == "d1"
    assert row["Status"] == "Drafted"


def test_lead_without_an_email_still_gets_whatsapp_text():
    sheets = FakeSheets(index([lead("a")]), index([research_row("a")]))
    gmail = FakeGmail()

    report = outreach_draft.run(cfg(), sheets, FakeWriter(), gmail)

    assert report.whatsapp_only == 1
    assert gmail.drafted == []
    row = sheets.upserts[0][1][0]
    assert row["Channel"] == "WhatsApp"
    assert row["WhatsApp Message"].startswith("Hi!")
    assert row["Gmail Draft ID"] == ""


def test_lead_with_no_email_and_no_whatsapp_is_unreachable():
    """Was named ...landline_phone... The number's shape decides nothing now:
    WhatsApp Business runs on landlines, so only a no from WhatsApp itself
    makes a lead unreachable."""
    sheets = FakeSheets(
        index([lead("a", phone="04-1234567")]),
        index([research_row("a", whatsapp="no")]),
    )
    gmail = FakeGmail()

    report = outreach_draft.run(cfg(), sheets, FakeWriter(), gmail)

    assert report.whatsapp_only == 0
    assert gmail.drafted == []
    row = sheets.upserts[0][1][0]
    assert row["Channel"] == "Unreachable"
    # Wording widened from "landline": in the US a number can be
    # unreachable on WhatsApp without being a landline at all.
    assert "not reachable on WhatsApp" in row["Notes"]
    assert row["Status"] == "Drafted"


def test_lead_with_email_and_landline_phone_still_gets_email():
    sheets = FakeSheets(
        index([lead("a", phone="04-1234567")]),
        index([research_row("a", "o@salon.my")]),
    )
    gmail = FakeGmail()

    report = outreach_draft.run(cfg(), sheets, FakeWriter(), gmail)

    assert report.drafts_created == 1
    row = sheets.upserts[0][1][0]
    assert row["Channel"] == "Email"


def test_the_footer_reaches_the_actual_draft_body():
    sheets = FakeSheets(index([lead("a")]), index([research_row("a", "o@salon.my")]))
    gmail = FakeGmail()
    outreach_draft.run(cfg(), sheets, FakeWriter(), gmail)

    _to, _subject, body = gmail.drafted[0]
    assert "12 Some Road" in body
    assert "Yiu Ming" in body


def test_preview_url_is_offered_to_the_model_when_a_site_exists():
    sheets = FakeSheets(
        index([lead("a")]),
        index([research_row("a")]),
        site_briefs=index([{"Lead ID": "a", "Preview URL": "https://preview.test/a"}]),
    )
    writer = FakeWriter()
    outreach_draft.run(cfg(), sheets, writer, FakeGmail())

    assert "https://preview.test/a" in writer.prompts[0]


def test_model_is_told_not_to_imply_a_site_when_there_is_none():
    sheets = FakeSheets(index([lead("a")]), index([research_row("a")]))
    writer = FakeWriter()
    outreach_draft.run(cfg(), sheets, writer, FakeGmail())

    assert "Do not imply one exists" in writer.prompts[0]


def test_rerun_skips_leads_that_already_have_a_message():
    sheets = FakeSheets(
        index([lead("a"), lead("b")]),
        index([research_row("a"), research_row("b")]),
        outreach=["a", "b"],
    )
    writer, gmail = FakeWriter(), FakeGmail()

    report = outreach_draft.run(cfg(), sheets, writer, gmail)

    assert report.selected == 0
    assert writer.call_count == 0
    assert gmail.drafted == []


def test_lead_that_has_not_been_researched_is_not_ready():
    sheets = FakeSheets(index([lead("a", stage="Discovered")]), {})
    report = outreach_draft.run(cfg(), sheets, FakeWriter(), FakeGmail())

    assert report.selected == 0
    assert report.not_ready == 1


def test_dry_run_creates_no_drafts_and_writes_nothing():
    sheets = FakeSheets(index([lead("a")]), index([research_row("a", "o@salon.my")]))
    gmail = FakeGmail()

    report = outreach_draft.run(cfg(), sheets, FakeWriter(), gmail, dry_run=True)

    assert report.copy_written == 1
    assert gmail.drafted == []
    assert sheets.upserts == []
    assert sheets.column_writes == []


def test_a_gmail_failure_is_logged_and_the_run_continues():
    class BrokenGmail(FakeGmail):
        def create_draft(self, *, to, subject, body):
            if to == "bad@salon.my":
                raise GmailError("Gmail said no")
            return super().create_draft(to=to, subject=subject, body=body)

    sheets = FakeSheets(
        index([lead("good"), lead("bad")]),
        index([research_row("good", "ok@salon.my"), research_row("bad", "bad@salon.my")]),
    )
    report = outreach_draft.run(cfg(), sheets, FakeWriter(), BrokenGmail())

    assert report.drafts_created == 1
    assert report.errors == 1
    written = {r["Lead ID"] for _tab, rows in sheets.upserts for r in rows}
    assert written == {"good"}


def test_stage_advances_to_email_drafted():
    sheets = FakeSheets(index([lead("a")]), index([research_row("a")]))
    outreach_draft.run(cfg(), sheets, FakeWriter(), FakeGmail())

    assert sheets.column_writes == [(schema.LEADS, "Stage", {"a": "Email Drafted"})]


# ------------------------------------------------ demo-first: require_site


def site_first():
    """The Penang campaign's real posture: build the demo before writing."""
    return config(postal_address="12 Some Road, Penang", require_site=True)


def test_require_site_holds_back_a_lead_with_no_demo_yet():
    sheets = FakeSheets(index([lead("a", stage="Researched")]), index([research_row("a")]))
    writer, gmail = FakeWriter(), FakeGmail()

    report = outreach_draft.run(site_first(), sheets, writer, gmail)

    assert report.selected == 0
    assert report.not_ready == 1
    assert writer.call_count == 0, "no Claude spend on a lead we can't pitch yet"
    assert gmail.drafted == []


def test_require_site_releases_the_lead_once_the_demo_exists():
    sheets = FakeSheets(
        index([lead("a", stage="Site Built")]),
        index([research_row("a", "o@salon.my")]),
        site_briefs=index([{"Lead ID": "a", "Preview URL": "https://preview.test/a"}]),
    )
    writer = FakeWriter()
    report = outreach_draft.run(site_first(), sheets, writer, FakeGmail())

    assert report.selected == 1
    assert "https://preview.test/a" in writer.prompts[0]


def test_without_require_site_a_researched_lead_is_written_to_immediately():
    sheets = FakeSheets(index([lead("a", stage="Researched")]), index([research_row("a")]))
    report = outreach_draft.run(cfg(), sheets, FakeWriter(), FakeGmail())
    assert report.selected == 1


def test_penang_campaign_is_configured_demo_first():
    """The one test that reads the campaign file's own values on purpose."""
    from outreach.config import load_config

    c = load_config("config/campaigns/penang-my.yaml", env_file=None)
    assert c.outreach.require_site is True, "no message goes out without a demo"
    assert c.lovable.auto_approve is False, "every prompt is reviewed by hand"


def test_a_lead_with_an_email_still_gets_a_site_screenshot(monkeypatch, tmp_path):
    """The second reason no picture appeared.

    The capture sat in the no-email branch, but a lead with an email is
    filed as Channel Email and still shows up in the WhatsApp queue. So for
    most leads the screenshot was never taken at all.
    """
    from outreach.stages import outreach_draft

    taken = []
    monkeypatch.setattr(
        outreach_draft,
        "capture_site_shot",
        lambda cfg, lead, lead_id, url="": taken.append(lead_id) or tmp_path / "s.png",
    )

    sheets = FakeSheets(
        index([lead("a")]),
        index([research_row("a", email="hi@salon.com")]),
    )
    report = outreach_draft.run(cfg(), sheets, FakeWriter(), FakeGmail())

    assert taken == ["a"], "an emailed lead must still be photographed"
    assert report.screenshots == 1


def test_a_lead_with_no_whatsapp_is_not_photographed(monkeypatch):
    """The picture rides on the WhatsApp message, so a lead that cannot
    receive one is a browser launch bought for nothing."""
    from outreach.stages import outreach_draft

    taken = []
    monkeypatch.setattr(
        outreach_draft,
        "capture_site_shot",
        lambda cfg, lead, lead_id, url="": taken.append(lead_id),
    )

    sheets = FakeSheets(
        index([lead("a")]),
        index([research_row("a", email="hi@salon.com", whatsapp="no")]),
    )
    outreach_draft.run(cfg(), sheets, FakeWriter(), FakeGmail())

    assert taken == []
