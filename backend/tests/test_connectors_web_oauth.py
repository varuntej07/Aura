from __future__ import annotations

import json
import urllib.parse
from unittest.mock import patch

from starlette.requests import Request

from src.handlers.connectors import _validate_web_oauth_request
from src.services.google_calendar_connector import GoogleCalendarConnector

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
