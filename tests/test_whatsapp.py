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
        assert is_mobile_number(phone) is True

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
        assert is_mobile_number(phone) is False


# ----------------------------------------------------------- dashboard API


def _make_client(monkeypatch, *, sheets=None, whatsapp_enabled=True):
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
                return {}

        client = _make_client(monkeypatch, sheets=FakeSheets())
        resp = client.get("/api/whatsapp/queue")
        data = json.loads(resp.data)
        assert data["queue"] == []

    def test_hides_landline_numbers(self, monkeypatch):
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
