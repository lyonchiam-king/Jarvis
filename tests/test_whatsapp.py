"""Tests for the WhatsApp provider and dashboard endpoints."""

import json

import pytest
import requests
import responses

from outreach.config import load_config
from outreach.dashboard import create_app
from outreach.providers.whatsapp import (
    SendResult,
    WhatsAppClient,
    WhatsAppError,
    is_mobile_number,
)


# ------------------------------------------------------------------ client


class TestWhatsAppClient:
    def test_is_connected_true_when_relay_reports_connected(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/status",
                json={"connected": True, "paired": True, "daily_sent": 5},
            )
            client = WhatsAppClient()
            assert client.is_connected() is True

    def test_is_connected_false_on_connection_error(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/status",
                body=requests.ConnectionError("refused"),
            )
            client = WhatsAppClient()
            assert client.is_connected() is False

    def test_is_connected_false_when_relay_says_disconnected(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/status",
                json={"connected": False},
            )
            client = WhatsAppClient()
            assert client.is_connected() is False

    def test_get_status_returns_full_blob(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/status",
                json={
                    "connected": True,
                    "paired": True,
                    "daily_sent": 12,
                    "daily_cap": 30,
                },
            )
            client = WhatsAppClient()
            status = client.get_status()
            assert status["connected"] is True
            assert status["daily_sent"] == 12

    def test_get_status_returns_error_dict_on_failure(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/status",
                body=requests.ConnectionError("refused"),
            )
            client = WhatsAppClient()
            status = client.get_status()
            assert status["connected"] is False
            assert "error" in status

    def test_get_qr_returns_data_url_when_unpaired(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/qr",
                json={"qr": "data:image/png;base64,iVBORw0KGgo="},
            )
            client = WhatsAppClient()
            assert client.get_qr() == "data:image/png;base64,iVBORw0KGgo="

    def test_get_qr_returns_none_when_paired(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/qr",
                json={"qr": None},
            )
            client = WhatsAppClient()
            assert client.get_qr() is None

    def test_get_qr_returns_none_on_error(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/qr",
                body=requests.ConnectionError("refused"),
            )
            client = WhatsAppClient()
            assert client.get_qr() is None

    def test_custom_port_is_respected(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:5000/status",
                json={"connected": True},
            )
            client = WhatsAppClient(port=5000)
            assert client.is_connected() is True


class TestSendMessage:
    def test_send_returns_result_on_success(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                json={
                    "status": "sent",
                    "message_id": "msg-abc-123",
                    "timestamp": "2026-08-14T10:30:00Z",
                },
            )
            client = WhatsAppClient()
            result = client.send_message("+60123456789", "Hello from COAI")
            assert isinstance(result, SendResult)
            assert result.status == "sent"
            assert result.message_id == "msg-abc-123"

    def test_send_raises_on_empty_phone(self):
        client = WhatsAppClient()
        with pytest.raises(WhatsAppError, match="phone is required"):
            client.send_message("", "Hello")

    def test_send_raises_on_whitespace_phone(self):
        client = WhatsAppClient()
        with pytest.raises(WhatsAppError, match="phone is required"):
            client.send_message("   ", "Hello")

    def test_send_raises_on_empty_message(self):
        client = WhatsAppClient()
        with pytest.raises(WhatsAppError, match="message text is required"):
            client.send_message("+60123456789", "")

    def test_send_raises_on_whitespace_message(self):
        client = WhatsAppClient()
        with pytest.raises(WhatsAppError, match="message text is required"):
            client.send_message("+60123456789", "   ")

    def test_send_raises_on_connection_error(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                body=requests.ConnectionError("refused"),
            )
            client = WhatsAppClient()
            with pytest.raises(WhatsAppError, match="relay is not running"):
                client.send_message("+60123456789", "Hello")

    def test_send_raises_on_daily_cap(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                status=429,
                json={"status": "failed", "error": "Daily cap reached"},
            )
            client = WhatsAppClient()
            with pytest.raises(WhatsAppError, match="Daily cap reached"):
                client.send_message("+60123456789", "Hello")

    def test_send_raises_on_not_connected(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                status=503,
                json={"status": "failed", "error": "not connected"},
            )
            client = WhatsAppClient()
            with pytest.raises(WhatsAppError, match="not connected"):
                client.send_message("+60123456789", "Hello")

    def test_send_raises_on_relay_reported_failure(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                json={"status": "failed", "error": "Invalid phone number"},
            )
            client = WhatsAppClient()
            with pytest.raises(WhatsAppError, match="Invalid phone number"):
                client.send_message("+60123456789", "Hello")

    def test_send_strips_phone_and_message_whitespace(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                json={"status": "sent", "message_id": "x", "timestamp": "t"},
            )
            client = WhatsAppClient()
            result = client.send_message("  +60123456789  ", "  Hello  ")
            assert result.status == "sent"


# ------------------------------------------------------- phone classification


class TestIsMobileNumber:
    @pytest.mark.parametrize(
        "phone",
        [
            "0123456789",
            "0133456789",
            "0143456789",
            "0173456789",
            "0193456789",
            "0113456789",  # "01_ others" — any 01X prefix
            "0183456789",
            "+6012-3456789",
            "60123456789",
            "+60 12 345 6789",
            "012-345 6789",
        ],
    )
    def test_mobile_numbers_are_sendable(self, phone):
        assert is_mobile_number(phone, "MY") is True

    @pytest.mark.parametrize(
        "phone",
        [
            "0345678901",  # landline
            "0445678901",  # landline
            "0545678901",  # landline
            "+603-45678901",
            "60445678901",
            "",
            "   ",
            "not-a-number",
        ],
    )
    def test_landlines_and_garbage_are_not_sendable(self, phone):
        assert is_mobile_number(phone, "MY") is False


# ----------------------------------------------------------- dashboard API


def _make_client(monkeypatch, *, sheets=None, whatsapp_enabled=True, output_dir=None):
    """Create a Flask test client with the outreach dashboard.

    Uses the real penang-my config as a base, then overrides WhatsApp settings
    and optionally patches the Sheets client.
    """
    from outreach import dashboard as dash

    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    # OutreachSettings and Secrets are frozen dataclasses — use
    # object.__setattr__ to bypass.
    object.__setattr__(config.outreach, "whatsapp_enabled", whatsapp_enabled)
    object.__setattr__(config.outreach, "whatsapp_relay_port", 4099)
    # sheet_id comes from .env, not the YAML, so it is None when env_file=None.
    # Set a fake one so the queue/send endpoints don't bail out early.
    object.__setattr__(config.secrets, "sheet_id", "fake-sheet-id")
    # Where built sites and their screenshots live. Overridable so a test
    # can use tmp_path without chdir'ing away from the campaign file.
    if output_dir is not None:
        object.__setattr__(config.build, "output_dir", str(output_dir))

    if sheets is not None:
        monkeypatch.setattr(dash, "_sheets_client", lambda c: sheets)

    return create_app(config).test_client()


class TestWhatsAppStatusEndpoint:
    def test_returns_disabled_when_not_enabled(self, monkeypatch):
        client = _make_client(monkeypatch, whatsapp_enabled=False)
        resp = client.get("/api/whatsapp/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data == {"enabled": False}

    def test_returns_relay_status_when_enabled(self, monkeypatch):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/status",
                json={"connected": True, "paired": True, "daily_sent": 3},
            )
            client = _make_client(monkeypatch)
            resp = client.get("/api/whatsapp/status")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["enabled"] is True
            assert data["connected"] is True

    def test_returns_error_when_relay_unreachable(self, monkeypatch):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/status",
                body=requests.ConnectionError("refused"),
            )
            client = _make_client(monkeypatch)
            resp = client.get("/api/whatsapp/status")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["connected"] is False


class TestWhatsAppQrEndpoint:
    def test_returns_null_when_not_enabled(self, monkeypatch):
        client = _make_client(monkeypatch, whatsapp_enabled=False)
        resp = client.get("/api/whatsapp/qr")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data == {"qr": None}

    def test_returns_qr_when_unpaired(self, monkeypatch):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/qr",
                json={"qr": "data:image/png;base64,ABC123"},
            )
            client = _make_client(monkeypatch)
            resp = client.get("/api/whatsapp/qr")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["qr"] == "data:image/png;base64,ABC123"

    def test_returns_null_on_relay_error(self, monkeypatch):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://127.0.0.1:4099/qr",
                body=requests.ConnectionError("refused"),
            )
            client = _make_client(monkeypatch)
            resp = client.get("/api/whatsapp/qr")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["qr"] is None


class TestWhatsAppQueueEndpoint:
    def test_returns_empty_when_no_sheet(self, monkeypatch):
        config = load_config("config/campaigns/penang-my.yaml", env_file=None)
        object.__setattr__(config.outreach, "whatsapp_enabled", True)
        object.__setattr__(config.secrets, "sheet_id", None)
        client = create_app(config).test_client()
        resp = client.get("/api/whatsapp/queue")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data == {"queue": [], "excluded": []}

    def test_returns_empty_when_sheet_has_no_whatsapp_rows(self, monkeypatch):
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.OUTREACH_LOG:
                    return {}
                if tab is schema.LEADS:
                    return {}
                if tab is schema.RESEARCH:
                    return {
                        k: (i + 2, {"Lead ID": k, "WhatsApp Reachable": "yes"})
                        for i, k in enumerate(self.read_rows(schema.LEADS))
                    }
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        resp = client.get("/api/whatsapp/queue")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data == {"queue": [], "excluded": []}

    def test_returns_whatsapp_drafts_with_phone(self, monkeypatch):
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.OUTREACH_LOG:
                    return {
                        "lead-1": (
                            2,
                            {
                                "Lead ID": "lead-1",
                                "Business Name": "Alice Salon",
                                "Channel": "WhatsApp",
                                "Status": "Drafted",
                                "WhatsApp Message": "Hi Alice, we built you a site!",
                                "WhatsApp Status": "",
                            },
                        ),
                        "lead-2": (
                            3,
                            {
                                "Lead ID": "lead-2",
                                "Business Name": "Bob Cuts",
                                "Channel": "Email",
                                "Status": "Drafted",
                                "WhatsApp Message": "Should not appear",
                            },
                        ),
                    }
                if tab is schema.LEADS:
                    return {
                        "lead-1": (
                            2,
                            {
                                "Lead ID": "lead-1",
                                "Business Name": "Alice Salon",
                                "Phone": "+6012-3456789",
                            },
                        ),
                    }
                if tab is schema.RESEARCH:
                    return {
                        k: (i + 2, {"Lead ID": k, "WhatsApp Reachable": "yes"})
                        for i, k in enumerate(self.read_rows(schema.LEADS))
                    }
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        resp = client.get("/api/whatsapp/queue")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["queue"]) == 1
        assert data["queue"][0]["lead_id"] == "lead-1"
        assert data["queue"][0]["name"] == "Alice Salon"
        assert data["queue"][0]["phone"] == "+6012-3456789"
        assert "we built you a site" in data["queue"][0]["message"]

    def test_accepts_whatsapp_row_with_empty_status_but_draft_markers(self, monkeypatch):
        # A row whose Status cell was cleared by hand still carries a drafted
        # message and a timestamp, so section 4 must offer it to send rather
        # than silently dropping the work the pipeline already did.
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.OUTREACH_LOG:
                    return {
                        "lead-1": (
                            2,
                            {
                                "Lead ID": "lead-1",
                                "Business Name": "Alice Salon",
                                "Channel": "WhatsApp",
                                "Status": "",
                                "WhatsApp Message": "Hi Alice, we built you a site!",
                                "WhatsApp Status": "",
                                "Drafted At": "2026-08-09 10:00:00",
                            },
                        ),
                    }
                if tab is schema.LEADS:
                    return {
                        "lead-1": (
                            2,
                            {
                                "Lead ID": "lead-1",
                                "Business Name": "Alice Salon",
                                "Phone": "+6012-3456789",
                            },
                        ),
                    }
                if tab is schema.RESEARCH:
                    return {
                        k: (i + 2, {"Lead ID": k, "WhatsApp Reachable": "yes"})
                        for i, k in enumerate(self.read_rows(schema.LEADS))
                    }
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        resp = client.get("/api/whatsapp/queue")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["queue"]) == 1
        assert data["queue"][0]["lead_id"] == "lead-1"
        assert "we built you a site" in data["queue"][0]["message"]

    def test_skips_already_sent_whatsapp(self, monkeypatch):
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.OUTREACH_LOG:
                    return {
                        "lead-1": (
                            2,
                            {
                                "Lead ID": "lead-1",
                                "Business Name": "Alice Salon",
                                "Channel": "WhatsApp",
                                "Status": "Drafted",
                                "WhatsApp Message": "Hi Alice!",
                                "WhatsApp Status": "sent",
                            },
                        ),
                    }
                if tab is schema.LEADS:
                    return {
                        "lead-1": (
                            2,
                            {"Lead ID": "lead-1", "Phone": "+60123456789"},
                        ),
                    }
                if tab is schema.RESEARCH:
                    return {
                        k: (i + 2, {"Lead ID": k, "WhatsApp Reachable": "yes"})
                        for i, k in enumerate(self.read_rows(schema.LEADS))
                    }
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        resp = client.get("/api/whatsapp/queue")
        data = json.loads(resp.data)
        assert data["queue"] == []

    def test_skips_leads_without_phone(self, monkeypatch):
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.OUTREACH_LOG:
                    return {
                        "lead-1": (
                            2,
                            {
                                "Lead ID": "lead-1",
                                "Business Name": "No Phone Salon",
                                "Channel": "WhatsApp",
                                "Status": "Drafted",
                                "WhatsApp Message": "Hi!",
                                "WhatsApp Status": "",
                            },
                        ),
                    }
                if tab is schema.LEADS:
                    return {
                        "lead-1": (
                            2,
                            {"Lead ID": "lead-1", "Phone": ""},
                        ),
                    }
                if tab is schema.RESEARCH:
                    return {
                        k: (i + 2, {"Lead ID": k, "WhatsApp Reachable": "yes"})
                        for i, k in enumerate(self.read_rows(schema.LEADS))
                    }
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        resp = client.get("/api/whatsapp/queue")
        data = json.loads(resp.data)
        assert data["queue"] == []

    def test_hides_numbers_whatsapp_said_no_to(self, monkeypatch):
        """Was test_hides_landline_numbers. The queue no longer hides a lead
        for having a landline-shaped number -- WhatsApp Business verifies
        fixed lines, and reading the shape as a verdict wrote off every UK
        business with an 020 number. Only a real no from WhatsApp hides."""
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.OUTREACH_LOG:
                    return {
                        "lead-1": (
                            2,
                            {
                                "Lead ID": "lead-1",
                                "Business Name": "Mobile Salon",
                                "Channel": "WhatsApp",
                                "Status": "Drafted",
                                "WhatsApp Message": "Hi mobile!",
                                "WhatsApp Status": "",
                            },
                        ),
                        "lead-2": (
                            3,
                            {
                                "Lead ID": "lead-2",
                                "Business Name": "Landline Salon",
                                "Channel": "WhatsApp",
                                "Status": "Drafted",
                                "WhatsApp Message": "Hi landline!",
                                "WhatsApp Status": "",
                            },
                        ),
                    }
                if tab is schema.LEADS:
                    return {
                        "lead-1": (
                            2,
                            {"Lead ID": "lead-1", "Phone": "+6012-3456789"},
                        ),
                        "lead-2": (
                            3,
                            {"Lead ID": "lead-2", "Phone": "04-1234567"},
                        ),
                    }
                if tab is schema.RESEARCH:
                    # lead-2 is hidden because WhatsApp said no, not because
                    # the number looks like a landline -- WhatsApp Business
                    # runs on landlines, so the shape decides nothing.
                    return {
                        "lead-1": (2, {"Lead ID": "lead-1", "WhatsApp Reachable": "yes"}),
                        "lead-2": (3, {"Lead ID": "lead-2", "WhatsApp Reachable": "no"}),
                    }
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        resp = client.get("/api/whatsapp/queue")
        data = json.loads(resp.data)
        assert len(data["queue"]) == 1
        assert data["queue"][0]["lead_id"] == "lead-1"
        # The landline is dropped, but not silently -- it lands in `excluded`
        # with the reason and the phone value the code actually saw.
        assert len(data["excluded"]) == 1
        assert data["excluded"][0]["lead_id"] == "lead-2"
        assert data["excluded"][0]["reason"] == "phone_not_mobile"
        assert data["excluded"][0]["phone"] == "04-1234567"

    def test_excluded_lists_every_reason_a_drafted_lead_is_hidden(self, monkeypatch):
        """A drafted WhatsApp lead can be hidden for four reasons. Each must
        show up in `excluded` with a human reason, so section 4 is never blank
        without explanation -- the 'my drafted message isn't showing' bug."""
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.OUTREACH_LOG:
                    return {
                        # Already sent -> already_sent.
                        "lead-sent": (
                            2,
                            {
                                "Lead ID": "lead-sent",
                                "Business Name": "Sent Salon",
                                "Channel": "WhatsApp",
                                "Status": "Drafted",
                                "WhatsApp Message": "Hi!",
                                "WhatsApp Status": "sent",
                            },
                        ),
                        # No phone in Leads -> phone_empty.
                        "lead-nophone": (
                            3,
                            {
                                "Lead ID": "lead-nophone",
                                "Business Name": "No Phone Salon",
                                "Channel": "WhatsApp",
                                "Status": "Drafted",
                                "WhatsApp Message": "Hi!",
                                "WhatsApp Status": "",
                            },
                        ),
                        # Landline -> phone_not_mobile.
                        "lead-landline": (
                            4,
                            {
                                "Lead ID": "lead-landline",
                                "Business Name": "Landline Salon",
                                "Channel": "WhatsApp",
                                "Status": "Drafted",
                                "WhatsApp Message": "Hi!",
                                "WhatsApp Status": "",
                            },
                        ),
                        # No matching row in Leads at all -> no_lead_row.
                        "lead-orphan": (
                            5,
                            {
                                "Lead ID": "lead-orphan",
                                "Business Name": "Orphan Salon",
                                "Channel": "WhatsApp",
                                "Status": "Drafted",
                                "WhatsApp Message": "Hi!",
                                "WhatsApp Status": "",
                            },
                        ),
                        # Mobile + drafted -> in the queue, not excluded.
                        "lead-ok": (
                            6,
                            {
                                "Lead ID": "lead-ok",
                                "Business Name": "OK Salon",
                                "Channel": "WhatsApp",
                                "Status": "Drafted",
                                "WhatsApp Message": "Hi mobile!",
                                "WhatsApp Status": "",
                            },
                        ),
                    }
                if tab is schema.LEADS:
                    return {
                        "lead-sent": (2, {"Lead ID": "lead-sent", "Phone": "+60123456789"}),
                        "lead-nophone": (3, {"Lead ID": "lead-nophone", "Phone": ""}),
                        "lead-landline": (4, {"Lead ID": "lead-landline", "Phone": "04-1234567"}),
                        # lead-orphan deliberately absent from Leads.
                        "lead-ok": (6, {"Lead ID": "lead-ok", "Phone": "+6012-3456789"}),
                    }
                if tab is schema.RESEARCH:
                    # lead-landline is excluded because WhatsApp answered no
                    # for it, not because of how the number looks.
                    return {
                        k: (
                            i + 2,
                            {
                                "Lead ID": k,
                                "WhatsApp Reachable": "no" if k == "lead-landline" else "yes",
                            },
                        )
                        for i, k in enumerate(self.read_rows(schema.LEADS))
                    }
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        resp = client.get("/api/whatsapp/queue")
        data = json.loads(resp.data)
        # The one good lead is in the queue.
        assert [q["lead_id"] for q in data["queue"]] == ["lead-ok"]
        # Each hidden lead is in `excluded` with its reason.
        by_id = {e["lead_id"]: e for e in data["excluded"]}
        assert set(by_id) == {"lead-sent", "lead-nophone", "lead-landline", "lead-orphan"}
        assert by_id["lead-sent"]["reason"] == "already_sent"
        assert by_id["lead-nophone"]["reason"] == "phone_empty"
        assert by_id["lead-landline"]["reason"] == "phone_not_mobile"
        assert by_id["lead-orphan"]["reason"] == "no_lead_row"
        # phone_not_mobile carries the phone so the user can see what was rejected.
        assert by_id["lead-landline"]["phone"] == "04-1234567"

    def test_every_drafted_row_is_either_queued_or_explained(self, monkeypatch):
        """The invariant behind 'however many I drafted, I still see them all'.

        Each row below used to fall through a bare ``continue``: an Email-
        channel draft, a row whose Channel cell is something else entirely,
        and a row carrying a message whose Status is not "Drafted" -- the mark
        a stale column order leaves. None of them can be sent from section 4,
        but all of them are work the pipeline did, so all of them have to be
        accounted for on screen rather than silently dropped.
        """
        from outreach import schema

        def row(n, lead_id, name, **over):
            base = {
                "Lead ID": lead_id,
                "Business Name": name,
                "Channel": "WhatsApp",
                "Status": "Drafted",
                "WhatsApp Message": "Hi!",
                "Drafted At": "2026-08-15 08:00:00Z",
                "WhatsApp Status": "",
            }
            base.update(over)
            return (n, base)

        drafted = {
            "lead-email": row(2, "lead-email", "Email Salon",
                              Channel="Email", Recipient="hi@salon.my"),
            "lead-weird": row(3, "lead-weird", "Weird Salon", Channel="Carrier Pigeon"),
            "lead-status": row(4, "lead-status", "Stale Salon",
                               Status="2026-08-15T07:37:54.028Z"),
            "lead-unreach": row(5, "lead-unreach", "Unreachable Salon",
                                Channel="Unreachable"),
            "lead-ok": row(6, "lead-ok", "OK Salon"),
        }
        # Carries neither a message nor a timestamp, so it is not drafted work
        # and passing over it is not a silent drop.
        drafted["lead-empty"] = (7, {"Lead ID": "lead-empty", "Business Name": "",
                                     "Channel": "", "Status": "",
                                     "WhatsApp Message": "", "Drafted At": ""})

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.OUTREACH_LOG:
                    return drafted
                if tab is schema.LEADS:
                    return {
                        "lead-email": (2, {"Phone": "+60123456789"}),
                        "lead-weird": (3, {"Phone": "+60123456789"}),
                        "lead-status": (4, {"Phone": "+60123456789"}),
                        "lead-unreach": (5, {"Phone": "04-1234567"}),
                        "lead-ok": (6, {"Phone": "+6012-3456789"}),
                        "lead-empty": (7, {"Phone": ""}),
                    }
                if tab is schema.RESEARCH:
                    return {
                        k: (i + 2, {"Lead ID": k, "WhatsApp Reachable": "yes"})
                        for i, k in enumerate(self.read_rows(schema.LEADS))
                    }
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        data = json.loads(client.get("/api/whatsapp/queue").data)

        assert [q["lead_id"] for q in data["queue"]] == ["lead-ok"]

        by_id = {e["lead_id"]: e for e in data["excluded"]}
        assert by_id["lead-email"]["reason"] == "email_channel"
        assert by_id["lead-email"]["recipient"] == "hi@salon.my"
        assert by_id["lead-weird"]["reason"] == "unknown_channel"
        assert by_id["lead-weird"]["channel"] == "Carrier Pigeon"
        assert by_id["lead-status"]["reason"] == "status_not_drafted"
        assert by_id["lead-status"]["status"] == "2026-08-15T07:37:54.028Z"
        assert by_id["lead-unreach"]["reason"] == "unreachable"
        assert by_id["lead-unreach"]["phone"] == "04-1234567"

        # The invariant itself: every row carrying drafted work is somewhere.
        seen = {q["lead_id"] for q in data["queue"]} | set(by_id)
        assert seen == set(drafted) - {"lead-empty"}

    def test_sheet_read_error_is_returned_not_swallowed(self, monkeypatch):
        """When the Sheet read itself throws, the endpoint must surface the
        error so the dashboard can show it -- not an empty queue that looks
        like 'nothing to send'. This is the silent-blank-section-4 case."""
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                raise RuntimeError("boom: sheet unreachable")

        client = _make_client(monkeypatch, sheets=FakeSheets())
        resp = client.get("/api/whatsapp/queue")
        # HTTP 200 by design (the JS treats non-200 as a network error);
        # the body carries the error string the panel renders.
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "error" in data
        assert "boom" in data["error"]
        assert data.get("queue") in (None, [])


class TestWhatsAppSendEndpoint:
    def test_returns_error_when_not_enabled(self, monkeypatch):
        client = _make_client(monkeypatch, whatsapp_enabled=False)
        resp = client.post(
            "/api/whatsapp/send",
            json={"lead_id": "x", "phone": "+60", "message": "Hi"},
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "not enabled" in data["error"]

    def test_returns_error_when_lead_id_missing(self, monkeypatch):
        client = _make_client(monkeypatch)
        resp = client.post(
            "/api/whatsapp/send",
            json={"phone": "+60", "message": "Hi"},
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "lead_id" in data["error"]

    def test_returns_error_when_phone_missing(self, monkeypatch):
        client = _make_client(monkeypatch)
        resp = client.post(
            "/api/whatsapp/send",
            json={"lead_id": "x", "message": "Hi"},
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "phone" in data["error"]

    def test_returns_error_when_message_missing(self, monkeypatch):
        client = _make_client(monkeypatch)
        resp = client.post(
            "/api/whatsapp/send",
            json={"lead_id": "x", "phone": "+60"},
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "message" in data["error"]

    def test_sends_and_records_success(self, monkeypatch):
        updates = {}

        class FakeSheets:
            def update_column(self, tab, col, mapping):
                updates[col] = mapping

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                json={
                    "status": "sent",
                    "message_id": "msg-xyz",
                    "timestamp": "2026-08-14T10:30:00Z",
                },
            )
            client = _make_client(monkeypatch, sheets=FakeSheets())
            resp = client.post(
                "/api/whatsapp/send",
                json={
                    "lead_id": "lead-1",
                    "phone": "+60123456789",
                    "message": "Hello!",
                },
            )
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data.get("status") == "sent"
            assert updates.get("WhatsApp Status") == {"lead-1": "sent"}
            assert updates.get("WhatsApp Sent At") == {
                "lead-1": "2026-08-14T10:30:00Z"
            }

    def test_records_failure_in_sheet(self, monkeypatch):
        updates = {}

        class FakeSheets:
            def update_column(self, tab, col, mapping):
                updates[col] = mapping

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                status=429,
                json={"status": "failed", "error": "Daily cap reached"},
            )
            client = _make_client(monkeypatch, sheets=FakeSheets())
            resp = client.post(
                "/api/whatsapp/send",
                json={
                    "lead_id": "lead-1",
                    "phone": "+60123456789",
                    "message": "Hello!",
                },
            )
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert "Daily cap" in data["error"]
            assert updates.get("WhatsApp Status") == {"lead-1": "failed"}


class TestTheNumberThatActuallyGetsDialled:
    """The relay used to force a "60" onto any number without one, so a US
    lead became a Malaysian stranger. It now refuses anything that is not
    already international -- which only works if the caller normalises, and
    for a while it did not: every Malaysian number on the Sheet is stored
    national ("012-3456789") and would have been refused outright.
    """

    def _sent_body(self, rsps):
        import json as _j
        return _j.loads(rsps.calls[0].request.body)

    def _ok(self, rsps):
        rsps.add(
            responses.POST,
            "http://127.0.0.1:4099/send",
            json={"status": "sent", "message_id": "m1", "timestamp": "t"},
        )

    def test_a_malaysian_national_number_is_dialled_internationally(self):
        with responses.RequestsMock() as rsps:
            self._ok(rsps)
            WhatsAppClient().send_message("012-345 6789", "Hi", "MY")
            assert self._sent_body(rsps)["phone"] == "60123456789"

    def test_a_us_number_never_gains_a_malaysian_prefix(self):
        """The exact number the old relay would have produced was
        6015045550123 -- a real line belonging to someone who is not this
        lead. Nothing may reintroduce that."""
        with responses.RequestsMock() as rsps:
            self._ok(rsps)
            WhatsAppClient().send_message("+1 504-555-0123", "Hi", "US")
            dialled = self._sent_body(rsps)["phone"]
            assert dialled == "15045550123"
            assert not dialled.startswith("60")

    def test_an_already_international_number_is_left_alone(self):
        with responses.RequestsMock() as rsps:
            self._ok(rsps)
            WhatsAppClient().send_message("+60 12-3456789", "Hi", "MY")
            assert self._sent_body(rsps)["phone"] == "60123456789"

    def test_a_number_with_no_resolvable_country_is_refused_not_guessed(self):
        """A bare national number and no country: 012-3456789 is Malaysian,
        British or neither depending on where you stand. Refusing costs one
        lead; guessing messages a stranger."""
        with pytest.raises(WhatsAppError, match="which country"):
            WhatsAppClient().send_message("012-345 6789", "Hi")


class TestTheLookupAsksAboutAPhoneNumber:
    """Baileys' onWhatsApp takes a phone number and builds its query by
    prefixing "+". Handed a JID it asks about "+4479...@s.whatsapp.net",
    which matches nothing -- so every lead in every country came back "not
    on WhatsApp". Malaysia hid it for weeks, because the numbering plan
    answered there and the lookup was never called.
    """

    def test_the_relay_is_sent_bare_digits_not_a_jid(self):
        import json as _j

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/check",
                json={"results": {"442079460958": {"registered": True}}},
            )
            WhatsAppClient().check_numbers(["442079460958"])
            sent = _j.loads(rsps.calls[0].request.body)

        assert sent["phones"] == ["442079460958"]
        assert not any("@" in p for p in sent["phones"]), (
            "a JID here is what made every lookup answer no"
        )

    def test_a_registered_number_reads_as_reachable(self):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/check",
                json={"results": {"442079460958": {"registered": True}}},
            )
            assert WhatsAppClient().check_numbers(["442079460958"]) == {
                "442079460958": True
            }

    def test_a_failed_lookup_is_unknown_rather_than_no(self):
        """null must not collapse to False: a lead retired by a network
        hiccup never comes back."""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/check",
                json={
                    "results": {
                        "442079460958": {"registered": None, "error": "timeout"}
                    }
                },
            )
            assert WhatsAppClient().check_numbers(["442079460958"]) == {
                "442079460958": None
            }

    def test_probe_asks_for_the_raw_reply(self):
        import json as _j

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/check",
                json={"results": {}},
            )
            WhatsAppClient().check_raw(["442079460958"])
            assert _j.loads(rsps.calls[0].request.body)["debug"] is True


class TestTheReachabilityPanel:
    """What Lyon reads to know who can be messaged before anything is built.

    It is a view over the cached verdicts and must never ask WhatsApp
    anything -- opening the dashboard is not a reason to spend lookups
    against the account.
    """

    def _sheets(self, rows):
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.LEADS:
                    return {
                        k: (i + 2, v["lead"]) for i, (k, v) in enumerate(rows.items())
                    }
                if tab is schema.RESEARCH:
                    return {
                        k: (i + 2, v["research"])
                        for i, (k, v) in enumerate(rows.items())
                    }
                return {}

        return FakeSheets()

    def _get(self, monkeypatch, rows):
        client = _make_client(monkeypatch, sheets=self._sheets(rows))
        return json.loads(client.get("/api/reachability").data)

    def test_it_sorts_leads_into_verified_rejected_and_waiting(self, monkeypatch):
        data = self._get(
            monkeypatch,
            {
                "yes": {
                    "lead": {"Lead ID": "yes", "Business Name": "Yes Ltd",
                             "Stage": "Researched", "Phone": "+44 20 7946 0958",
                             "Country": "GB", "WhatsApp Reachable": "yes"},
                    "research": {},
                },
                "no": {
                    "lead": {"Lead ID": "no", "Business Name": "No Ltd",
                             "Stage": "Researched", "Phone": "+44 20 7946 0959",
                             "Country": "GB", "WhatsApp Reachable": "no"},
                    "research": {},
                },
                # Discovered, not researched, and never checked -- the case
                # this panel now exists to surface before research is spent.
                "new": {
                    "lead": {"Lead ID": "new", "Business Name": "New Ltd",
                             "Stage": "Discovered", "Phone": "+44 20 7946 0960",
                             "Country": "GB"},
                    "research": {},
                },
            },
        )

        assert data["counts"]["verified"] == 1
        assert data["counts"]["rejected"] == 1
        assert data["counts"]["waiting"] == 1

    def test_it_shows_the_number_as_it_will_actually_be_dialled(self, monkeypatch):
        """The whole class of bug here was numbers reaching WhatsApp in the
        wrong shape. Showing the dialled form makes a bad one visible
        instead of something to discover after a build."""
        data = self._get(
            monkeypatch,
            {
                "a": {
                    "lead": {"Lead ID": "a", "Business Name": "Chip Shop",
                             "Stage": "Researched", "Phone": "020 7946 0958",
                             "Country": "GB", "WhatsApp Reachable": "yes"},
                    "research": {},
                }
            },
        )

        assert data["verified"][0]["dialled"] == "442079460958"

    def test_a_rejected_lead_with_an_email_still_counts_as_buildable(self, monkeypatch):
        """Not on WhatsApp is not the same as not worth building for -- the
        gate lets an email through, and the panel must agree with the gate
        or it will look like leads vanished."""
        data = self._get(
            monkeypatch,
            {
                "a": {
                    "lead": {"Lead ID": "a", "Business Name": "Mailer",
                             "Stage": "Researched", "Phone": "+44 20 7946 0958",
                             "Country": "GB", "WhatsApp Reachable": "no"},
                    "research": {"Email Found": "hi@mailer.co.uk"},
                }
            },
        )

        assert data["counts"]["rejected"] == 1
        assert data["counts"]["buildable"] == 1

    def test_a_discovered_lead_is_listed_before_it_is_researched(self, monkeypatch):
        """Was test_an_unresearched_lead_is_not_listed_at_all.

        The premise reversed deliberately. Asking WhatsApp costs a lookup and
        researching costs a model call per lead, so the cheap question is
        answered first -- and this panel is what tells Lyon which leads are
        worth researching at all.
        """
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.LEADS:
                    return {
                        "a": (2, {"Lead ID": "a", "Stage": "Discovered",
                                  "Phone": "+44 20 7946 0958", "Country": "GB"})
                    }
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        data = json.loads(client.get("/api/reachability").data)

        assert data["counts"]["waiting"] == 1

    def test_a_lead_past_the_brief_stage_is_not_listed(self, monkeypatch):
        """The panel is about the decision still to be made. A lead already
        briefed or built is answered and only adds noise."""
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.LEADS:
                    return {
                        "a": (2, {"Lead ID": "a", "Stage": "Site Briefed",
                                  "Phone": "+44 20 7946 0958", "Country": "GB"})
                    }
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        data = json.loads(client.get("/api/reachability").data)

        assert data["counts"] == {
            "verified": 0, "rejected": 0, "waiting": 0, "buildable": 0
        }


class TestTheNumberIsNeverPrefixedTwice:
    """A send reported "sent" and was delivered to nobody.

    The number is normalised in the dashboard queue, using the lead's
    country. It was then normalised AGAIN in send_message, using the
    campaign's country -- and the second pass glued that country's code onto
    a number that already carried its own. A UK lead went out as
    60447450225230. Baileys accepts any well-formed JID and returns a
    message object, so the relay answered "sent" and nothing ever arrived.
    Malaysia survived by luck: 60... already started with 60.
    """

    def _sent_phone(self, rsps):
        import json as _j
        return _j.loads(rsps.calls[0].request.body)["phone"]

    def _ok(self, rsps):
        rsps.add(
            responses.POST,
            "http://127.0.0.1:4099/send",
            json={"status": "sent", "message_id": "m1", "timestamp": "t"},
        )

    def test_an_already_dialled_number_is_left_alone(self):
        """country="" is how the caller says "this is already E.164"."""
        with responses.RequestsMock() as rsps:
            self._ok(rsps)
            WhatsAppClient().send_message("447450225230", "Hi", "")
            assert self._sent_phone(rsps) == "447450225230"

    def test_a_uk_number_never_gains_a_malaysian_prefix(self):
        """The exact failure: 447450225230 must not become 60447450225230."""
        with responses.RequestsMock() as rsps:
            self._ok(rsps)
            WhatsAppClient().send_message("447450225230", "Hi", "")
            dialled = self._sent_phone(rsps)

        assert dialled == "447450225230"
        assert not dialled.startswith("60"), "a second country code was applied"

    def test_a_us_national_number_keeps_its_own_country_code(self):
        """504 is also Honduras' dialling code, so a US national number can
        look international to a digits-only guess. It must still gain the 1."""
        with responses.RequestsMock() as rsps:
            self._ok(rsps)
            WhatsAppClient().send_message("(504) 555-0123", "Hi", "US")
            assert self._sent_phone(rsps) == "15045550123"

    def test_a_national_number_still_gets_its_country(self):
        with responses.RequestsMock() as rsps:
            self._ok(rsps)
            WhatsAppClient().send_message("012-345 6789", "Hi", "MY")
            assert self._sent_phone(rsps) == "60123456789"


class TestTheDashboardSendPath:
    """Where the "sent but never delivered" bug actually lived.

    Testing send_message directly does not reach it: that function was fine.
    The fault was the dashboard handing it an already-E.164 number together
    with the CAMPAIGN's country, so it normalised a second time and prefixed
    a code the number already had.
    """

    def _relay_saw(self, monkeypatch, payload):
        """POST to the real endpoint; capture what reached WhatsAppClient."""
        from outreach import dashboard as dash

        seen = {}

        class FakeClient:
            def __init__(self, port=4099):
                pass

            def send_message(self, phone, text, country="", image_path=None):
                seen["image"] = image_path
                from outreach import phone as phone_lib
                from outreach.providers.whatsapp import SendResult

                # Mirror the real client: it normalises with what it is given.
                seen["dialled"] = phone_lib.normalise(phone, country)
                return SendResult(message_id="m1", status="sent", timestamp="t")

        class FakeSheets:
            def read_rows(self, tab):
                return {}

            def update_column(self, *a, **k):
                return 0

        monkeypatch.setattr(dash, "_sheets_client", lambda c: FakeSheets())
        import outreach.providers.whatsapp as wa

        monkeypatch.setattr(wa, "WhatsAppClient", FakeClient)
        client = _make_client(monkeypatch, sheets=FakeSheets())
        client.post(
            "/api/whatsapp/send",
            data=json.dumps(payload),
            content_type="application/json",
        )
        return seen.get("dialled")

    def test_a_uk_lead_is_not_dialled_as_malaysian(self, monkeypatch):
        """447450225230 must not go out as 60447450225230 -- a JID Baileys
        accepts, reports as sent, and delivers to nobody."""
        dialled = self._relay_saw(
            monkeypatch,
            {
                "lead_id": "a",
                "phone": "07450 225230",
                "phone_e164": "447450225230",
                "message": "Hi",
            },
        )

        assert dialled == "447450225230"
        assert not (dialled or "").startswith("60")

    def test_a_malaysian_lead_still_dials_correctly(self, monkeypatch):
        dialled = self._relay_saw(
            monkeypatch,
            {
                "lead_id": "a",
                "phone": "012-345 6789",
                "phone_e164": "60123456789",
                "message": "Hi",
            },
        )

        assert dialled == "60123456789"

    def test_without_a_dialled_number_the_campaign_country_is_used(self, monkeypatch):
        """The fallback still has to work: a raw display number carries no
        country, so the campaign's is the only thing left."""
        dialled = self._relay_saw(
            monkeypatch,
            {"lead_id": "a", "phone": "012-345 6789", "message": "Hi"},
        )

        assert dialled == "60123456789"


class TestSentMeansAcknowledged:
    """"Sent" reported a message that reached nobody.

    sendMessage() resolving only means the socket accepted the message. A
    half-open connection queues it, resolves, and transmits nothing -- so the
    dashboard said success while the sender's own phone showed no chat at
    all. The relay now waits for WhatsApp to acknowledge, and anything else
    is reported as a failure so the lead stays in the queue.
    """

    def _reply(self, rsps, body):
        rsps.add(responses.POST, "http://127.0.0.1:4099/send", json=body)

    def test_an_acknowledged_message_is_sent(self):
        with responses.RequestsMock() as rsps:
            self._reply(rsps, {"status": "sent", "message_id": "m1",
                               "timestamp": "t", "dialled": "447450225230", "ack": 1})
            result = WhatsAppClient().send_message("447450225230", "Hi", "")

        assert result.status == "sent"
        assert result.dialled == "447450225230"

    def test_an_unconfirmed_message_is_not_treated_as_a_failure(self):
        """This rule was briefly the other way round and stopped the queue.

        Requiring an ack turned every send into an error whenever WhatsApp
        confirmed late -- which it routinely does for a first message to a
        new contact. Silence is missing evidence, not evidence of nothing,
        so the send stands and the uncertainty is carried on the result.
        """
        with responses.RequestsMock() as rsps:
            self._reply(rsps, {"status": "unconfirmed", "message_id": "m1",
                               "timestamp": "t", "dialled": "447450225230",
                               "ack": None,
                               "note": "WhatsApp has not confirmed delivery yet."})
            result = WhatsAppClient().send_message("447450225230", "Hi", "")

        assert result.status == "unconfirmed"
        assert "not confirmed" in result.note

    def test_a_real_relay_failure_is_still_raised(self):
        """Softening the ack must not soften an actual refusal."""
        with responses.RequestsMock() as rsps:
            self._reply(rsps, {"status": "failed", "dialled": "447450225230",
                               "error": "WhatsApp is not connected"})
            with pytest.raises(WhatsAppError, match="not connected"):
                WhatsAppClient().send_message("447450225230", "Hi", "")

    def test_the_dialled_number_comes_back_either_way(self):
        """So a wrong number stays visible rather than being deduced from a
        message that never arrives."""
        with responses.RequestsMock() as rsps:
            self._reply(rsps, {"status": "unconfirmed", "message_id": "m1",
                               "timestamp": "t", "dialled": "60447450225230"})
            result = WhatsAppClient().send_message("60447450225230", "Hi", "")

        assert result.dialled == "60447450225230"


class TestTheScreenshotReachesWhatsApp:
    def test_an_image_is_sent_as_base64_with_the_text_as_caption(self, tmp_path):
        import json as _j

        shot = tmp_path / "screenshot.png"
        shot.write_bytes(b"\x89PNG-real-bytes")

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                json={"status": "sent", "message_id": "m1", "timestamp": "t",
                      "dialled": "60123456789", "ack": 1},
            )
            WhatsAppClient().send_message("60123456789", "Hi", "", image_path=shot)
            body = _j.loads(rsps.calls[0].request.body)

        import base64

        assert base64.b64decode(body["image"]) == b"\x89PNG-real-bytes"
        assert body["message"] == "Hi", "the text still travels as the caption"

    def test_an_empty_file_is_not_attached(self, tmp_path):
        """A zero-byte screenshot would send as a broken image."""
        import json as _j

        shot = tmp_path / "screenshot.png"
        shot.write_bytes(b"")

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                json={"status": "sent", "message_id": "m1", "timestamp": "t",
                      "dialled": "60123456789", "ack": 1},
            )
            WhatsAppClient().send_message("60123456789", "Hi", "", image_path=shot)
            body = _j.loads(rsps.calls[0].request.body)

        assert "image" not in body


class TestSeeingTheScreenshotBeforeSending:
    """Until this existed the picture was invisible.

    It is a file on disk, deliberately not a Sheet column, so the only way
    to find out whether one had been taken was to send the message and look
    at your phone -- which is the wrong order for something you might want
    to stop.
    """

    def _sheets(self, lead):
        from outreach import schema

        class FakeSheets:
            def read_rows(self, tab):
                if tab is schema.LEADS:
                    return {"lead-1": (2, lead)}
                return {}

        return FakeSheets()

    def test_a_missing_screenshot_is_reported_not_served_blank(self, monkeypatch):
        client = _make_client(
            monkeypatch,
            sheets=self._sheets({"Lead ID": "lead-1", "Business Name": "Salon"}),
        )
        resp = client.get("/api/screenshot/lead-1")

        assert resp.status_code == 404
        assert "No screenshot" in json.loads(resp.data)["error"]

    def test_an_unknown_lead_is_refused(self, monkeypatch):
        client = _make_client(
            monkeypatch,
            sheets=self._sheets({"Lead ID": "lead-1", "Business Name": "Salon"}),
        )
        assert client.get("/api/screenshot/nope").status_code == 404

    def test_the_path_comes_from_the_lead_never_from_the_url(
        self, monkeypatch, tmp_path
    ):
        """A filename off the wire would let any file on disk be read. The
        lead id only selects a row; the path is derived from its slug."""
        client = _make_client(
            monkeypatch,
            sheets=self._sheets({"Lead ID": "lead-1", "Business Name": "Salon"}),
            output_dir=tmp_path,
        )

        resp = client.get("/api/screenshot/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code == 404

    def test_a_real_screenshot_is_served(self, monkeypatch, tmp_path):
        from outreach.slug import site_slug

        lead = {"Lead ID": "lead-1", "Business Name": "Salon"}
        shot = tmp_path / site_slug("Salon", "lead-1") / "screenshot.png"
        shot.parent.mkdir(parents=True)
        shot.write_bytes(b"\x89PNG-bytes")

        client = _make_client(
            monkeypatch, sheets=self._sheets(lead), output_dir=tmp_path
        )
        resp = client.get("/api/screenshot/lead-1")

        assert resp.status_code == 200
        assert resp.data == b"\x89PNG-bytes"


class TestAnUnconfirmedSendStillCountsAsSent:
    """The regression that stopped the queue.

    Requiring a delivery acknowledgement turned working sends into errors:
    WhatsApp confirms late often enough -- particularly a first message to a
    new contact -- that within the eight-second window there is frequently
    nothing to hear. The send stands; the uncertainty is reported.
    """

    def _send(self, monkeypatch, relay_reply):
        from outreach import dashboard as dash
        from outreach.providers.whatsapp import SendResult

        written = {}

        class FakeClient:
            def __init__(self, port=4099):
                pass

            def send_message(self, phone, text, country="", image_path=None):
                return SendResult(**relay_reply)

        class FakeSheets:
            def read_rows(self, tab):
                return {}

            def update_column(self, tab, column, values, **k):
                written[column] = values
                return 1

        monkeypatch.setattr(dash, "_sheets_client", lambda c: FakeSheets())
        import outreach.providers.whatsapp as wa

        monkeypatch.setattr(wa, "WhatsAppClient", FakeClient)
        client = _make_client(monkeypatch, sheets=FakeSheets())
        resp = client.post(
            "/api/whatsapp/send",
            data=json.dumps(
                {"lead_id": "L1", "phone": "012-345 6789",
                 "phone_e164": "60123456789", "message": "Hi"}
            ),
            content_type="application/json",
        )
        return json.loads(resp.data), written

    def test_it_is_not_reported_as_an_error(self, monkeypatch):
        body, _written = self._send(
            monkeypatch,
            {"message_id": "m1", "status": "unconfirmed", "timestamp": "t",
             "dialled": "60123456789",
             "note": "WhatsApp has not confirmed delivery yet."},
        )

        assert "error" not in body, "an unconfirmed send is not a failure"
        assert body["ok"] is True

    def test_the_uncertainty_reaches_the_page(self, monkeypatch):
        """Silently calling it sent would be the older bug returning."""
        body, _written = self._send(
            monkeypatch,
            {"message_id": "m1", "status": "unconfirmed", "timestamp": "t",
             "dialled": "60123456789",
             "note": "WhatsApp has not confirmed delivery yet."},
        )

        assert "not confirmed" in body["note"]

    def test_the_lead_is_marked_done_so_it_leaves_the_queue(self, monkeypatch):
        """It was sent. Leaving it queued invites sending it twice."""
        _body, written = self._send(
            monkeypatch,
            {"message_id": "m1", "status": "unconfirmed", "timestamp": "t",
             "dialled": "60123456789", "note": "not confirmed"},
        )

        assert written["WhatsApp Status"] == {"L1": "unconfirmed"}

    def test_a_confirmed_send_carries_no_note(self, monkeypatch):
        body, _written = self._send(
            monkeypatch,
            {"message_id": "m1", "status": "sent", "timestamp": "t",
             "dialled": "60123456789"},
        )

        assert body["status"] == "sent"
        assert not body["note"]
