from __future__ import annotations

import json
import urllib.parse
from unittest.mock import patch

from starlette.requests import Request

from src.handlers.connectors import _validate_web_oauth_request
from src.services.gmail_connector import GmailConnector, GmailReauthorizationRequired
from src.services.google_calendar_connector import (
    GoogleCalendarConnector,
    GoogleCalendarReauthorizationRequired,
)

ALLOWED_ORIGIN = "https://auravoiceapp.com"


def _request(*, origin: str | None, requested_with: str | None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if origin:
        headers.append((b"origin", origin.encode()))
    if requested_with:
        headers.append((b"x-requested-with", requested_with.encode()))
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/connectors/google-calendar/connect",
        "headers": headers,
        "scheme": "https",
        "server": ("auravoiceapp.com", 443),
        "client": ("127.0.0.1", 1234),
        "query_string": b"",
    })


def test_web_oauth_accepts_matching_allowlisted_origin(monkeypatch):
    from src.handlers import connectors

    monkeypatch.setattr(connectors.settings, "CORS_ALLOWED_ORIGINS", ALLOWED_ORIGIN)
    request = _request(origin=ALLOWED_ORIGIN, requested_with="XMLHttpRequest")
    assert _validate_web_oauth_request(request, ALLOWED_ORIGIN) is None


def test_web_oauth_rejects_mismatched_origin(monkeypatch):
    from src.handlers import connectors

    monkeypatch.setattr(connectors.settings, "CORS_ALLOWED_ORIGINS", ALLOWED_ORIGIN)
    request = _request(origin="https://evil.example", requested_with="XMLHttpRequest")
    assert "origin" in _validate_web_oauth_request(request, ALLOWED_ORIGIN).lower()


def test_web_oauth_rejects_missing_csrf_header(monkeypatch):
    from src.handlers import connectors

    monkeypatch.setattr(connectors.settings, "CORS_ALLOWED_ORIGINS", ALLOWED_ORIGIN)
    request = _request(origin=ALLOWED_ORIGIN, requested_with=None)
    assert "X-Requested-With" in _validate_web_oauth_request(request, ALLOWED_ORIGIN)


def test_native_oauth_without_redirect_remains_supported():
    request = _request(origin=None, requested_with=None)
    assert _validate_web_oauth_request(request, None) is None


def test_token_exchange_uses_web_popup_origin(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"access_token": "access", "refresh_token": "refresh"}).encode()

    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = urllib.parse.parse_qs(request.data.decode())
        captured["timeout"] = timeout
        return Response()

    with patch("src.services.google_calendar_connector.urllib.request.urlopen", fake_urlopen):
        GoogleCalendarConnector("user-1")._exchange_server_auth_code(
            "server-code",
            redirect_uri=ALLOWED_ORIGIN,
        )

    assert captured["body"]["redirect_uri"] == [ALLOWED_ORIGIN]
    assert captured["body"]["code"] == ["server-code"]
    assert captured["timeout"] == 10


def test_status_reports_saved_credentials_as_reconnectable(monkeypatch):
    connector = GoogleCalendarConnector("user-1")
    monkeypatch.setattr(
        connector,
        "_load_integration",
        lambda: {"enabled": False, "refresh_token": "saved-refresh"},
    )
    monkeypatch.setattr(connector, "_load_source", lambda: {})

    status = connector.get_status()

    assert status["enabled"] is False
    assert status["can_reconnect"] is True


def test_enable_requires_saved_google_credentials(monkeypatch):
    connector = GoogleCalendarConnector("user-1")
    monkeypatch.setattr(connector, "_load_integration", lambda: {"enabled": False})

    try:
        connector.enable(watch_url=None)
    except GoogleCalendarReauthorizationRequired:
        pass
    else:
        raise AssertionError("enable should require Google reauthorization")


def test_enable_syncs_before_marking_connector_enabled(monkeypatch):
    writes: list[dict] = []
    connector = GoogleCalendarConnector("user-1")
    monkeypatch.setattr(
        connector,
        "_load_integration",
        lambda: {"enabled": False, "refresh_token": "saved-refresh"},
    )
    monkeypatch.setattr(
        connector,
        "_sync_calendar",
        lambda **kwargs: writes.append({"sync": kwargs}),
    )
    monkeypatch.setattr(
        connector,
        "_integration_ref",
        lambda: type(
            "Ref",
            (),
            {"set": lambda _self, payload, merge: writes.append(payload)},
        )(),
    )
    monkeypatch.setattr(
        connector,
        "get_status",
        lambda: {"enabled": True, "can_reconnect": True},
    )

    status = connector.enable(watch_url=None)

    assert writes[0] == {
        "sync": {"reason": "manual_reenable", "force_full_sync": True}
    }
    assert writes[1]["enabled"] is True
    assert status["enabled"] is True


def test_refreshing_saved_credentials_does_not_optimistically_enable(monkeypatch):
    persisted: list[dict] = []
    connector = GoogleCalendarConnector("user-1")

    class Credentials:
        valid = False
        expired = True
        refresh_token = "saved-refresh"
        token = "old-access"
        expiry = None

        def refresh(self, _request):
            self.valid = True
            self.expired = False
            self.token = "new-access"

    credentials = Credentials()
    monkeypatch.setattr(
        connector,
        "_load_integration",
        lambda: {"enabled": False, "refresh_token": "saved-refresh"},
    )
    monkeypatch.setattr(
        connector,
        "_credentials_from_integration",
        lambda: credentials,
    )
    monkeypatch.setattr(
        connector,
        "_persist_credentials",
        lambda **payload: persisted.append(payload),
    )
    monkeypatch.setattr(
        "src.services.google_calendar_connector.build",
        lambda *_args, **_kwargs: "calendar-client",
    )

    client = connector._calendar_client(refresh=True)

    assert client == "calendar-client"
    assert persisted[0]["access_token"] == "new-access"
    assert persisted[0]["enabled"] is False


def test_disable_retains_credentials_and_removes_active_state(monkeypatch):
    integration_writes: list[dict] = []
    deleted: list[str] = []
    connector = GoogleCalendarConnector("user-1")
    monkeypatch.setattr(connector, "_load_source", lambda: {})
    monkeypatch.setattr(
        connector,
        "_integration_ref",
        lambda: type(
            "IntegrationRef",
            (),
            {
                "set": lambda _self, payload, merge: integration_writes.append(payload),
            },
        )(),
    )
    monkeypatch.setattr(
        connector,
        "_job_ref",
        lambda: type(
            "JobRef",
            (),
            {"delete": lambda _self: deleted.append("job")},
        )(),
    )
    monkeypatch.setattr(
        connector,
        "_source_ref",
        lambda: type(
            "SourceRef",
            (),
            {"delete": lambda _self: deleted.append("source")},
        )(),
    )
    monkeypatch.setattr(
        connector,
        "_purge_calendar_cache",
        lambda: deleted.append("events"),
    )
    monkeypatch.setattr(
        connector,
        "get_status",
        lambda: {"enabled": False, "can_reconnect": True},
    )

    status = connector.disable()

    assert deleted == ["job", "events", "source"]
    assert integration_writes[0]["enabled"] is False
    assert "refresh_token" not in integration_writes[0]
    assert status["can_reconnect"] is True


def test_gmail_enable_requires_saved_credentials(monkeypatch):
    connector = GmailConnector("user-1")
    monkeypatch.setattr(connector, "_load_integration", lambda: {"enabled": False})

    try:
        connector.enable()
    except GmailReauthorizationRequired:
        pass
    else:
        raise AssertionError("enable should require Gmail reauthorization")


def test_gmail_disable_retains_credentials(monkeypatch):
    writes: list[dict] = []
    connector = GmailConnector("user-1")
    monkeypatch.setattr(
        connector,
        "_integration_ref",
        lambda: type(
            "Ref",
            (),
            {"set": lambda _self, payload, merge: writes.append(payload)},
        )(),
    )
    monkeypatch.setattr(
        connector,
        "get_status",
        lambda: {"enabled": False, "can_reconnect": True},
    )

    status = connector.disable()

    assert writes[0]["enabled"] is False
    assert "refresh_token" not in writes[0]
    assert status["can_reconnect"] is True
