from __future__ import annotations

import urllib.parse

from src.handlers import connector_oauth
from src.services import connector_oauth as connector_oauth_service


def test_authorization_url_is_connector_scoped_and_backend_owned(monkeypatch):
    monkeypatch.setattr(connector_oauth_service.settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(
        connector_oauth_service.settings,
        "GOOGLE_REDIRECT_URI",
        "https://backend.example/connectors/oauth/google/callback",
    )

    url = connector_oauth_service._authorization_url(
        connector="gmail",
        state="state-token",
        code_challenge="challenge",
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    assert query["scope"] == [connector_oauth_service.GMAIL_SCOPE]
    assert query["redirect_uri"] == [
        "https://backend.example/connectors/oauth/google/callback"
    ]
    assert query["state"] == ["state-token"]
    assert query["access_type"] == ["offline"]
    assert query["include_granted_scopes"] == ["true"]
    assert query["code_challenge"] == ["challenge"]
    assert query["code_challenge_method"] == ["S256"]


def test_calendar_authorization_does_not_request_gmail_scope(monkeypatch):
    monkeypatch.setattr(connector_oauth_service.settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(
        connector_oauth_service.settings,
        "GOOGLE_REDIRECT_URI",
        "https://backend.example/connectors/oauth/google/callback",
    )

    url = connector_oauth_service._authorization_url(
        connector="google_calendar",
        state="state-token",
        code_challenge="challenge",
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    assert query["scope"] == [connector_oauth_service.CALENDAR_SCOPE]
    assert connector_oauth_service.GMAIL_SCOPE not in query["scope"]


def test_completion_url_contains_only_routing_state():
    url = connector_oauth._completion_url(
        attempt_id="attempt",
        connector="gmail",
        outcome="success",
    )
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)

    assert parsed.scheme == "aura"
    assert parsed.netloc == "connectors"
    assert query == {
        "attempt_id": ["attempt"],
        "connector": ["gmail"],
        "outcome": ["success"],
    }
    assert "code" not in query
    assert "token" not in query


def test_pkce_pair_matches_s256_challenge():
    verifier, challenge = connector_oauth_service._pkce_pair()

    assert 43 <= len(verifier) <= 128
    assert "=" not in challenge
    assert len(challenge) == 43
