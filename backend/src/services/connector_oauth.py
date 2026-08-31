"""
OAuth attempt lifecycle for the backend-owned Google connector handoff.

Owns the connector_oauth_attempts schema, PKCE generation, the transactional
claim state machine (pending/processing/completed/expired), attempt
finalization, and the per-connector connect dispatch. The HTTP handler in
handlers/connector_oauth.py keeps request parsing, auth, and the HTML
completion page.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from google.cloud import firestore as gcloud_firestore

from ..config.settings import settings
from .firebase import admin_firestore
from .gmail_connector import GMAIL_SCOPES, GmailConnector
from .google_calendar_connector import CALENDAR_SCOPE, GoogleCalendarConnector

ATTEMPTS_COLLECTION = "connector_oauth_attempts"
ATTEMPT_TTL_SECONDS = 10 * 60
GMAIL_SCOPE = " ".join(GMAIL_SCOPES)
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

ATTEMPT_ID_TOKEN_BYTES = 32
# Derived once from the generator below so the callback's state-length check
# can never drift from what secrets.token_urlsafe(ATTEMPT_ID_TOKEN_BYTES) emits.
ATTEMPT_ID_LENGTH = len(secrets.token_urlsafe(ATTEMPT_ID_TOKEN_BYTES))

ConnectorName = Literal["google_calendar", "gmail"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _scope_for(connector: ConnectorName) -> str:
    return CALENDAR_SCOPE if connector == "google_calendar" else GMAIL_SCOPE


def _authorization_url(
    *,
    connector: ConnectorName,
    state: str,
    code_challenge: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": _scope_for(connector),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"


def resolve_watch_url(*, proto: str | None, host: str | None) -> str | None:
    """Resolve the calendar webhook watch URL from settings or forwarded headers."""
    if settings.GOOGLE_CALENDAR_WEBHOOK_URL:
        return settings.GOOGLE_CALENDAR_WEBHOOK_URL

    if proto == "https" and host:
        return f"https://{host}/integrations/google-calendar/webhook"

    return None


def create_attempt(*, user_id: str, connector: ConnectorName) -> tuple[str, str]:
    """Create a pending OAuth attempt; return (attempt_id, authorization_url).

    Raises on Firestore failure; the caller owns logging and the HTTP response.
    """
    attempt_id = secrets.token_urlsafe(ATTEMPT_ID_TOKEN_BYTES)
    verifier, challenge = _pkce_pair()
    now = _utc_now()
    expires_at = now + timedelta(seconds=ATTEMPT_TTL_SECONDS)
    attempt = {
        "uid": user_id,
        "connector": connector,
        "status": "pending",
        "code_verifier": verifier,
        "created_at": now,
        "expires_at": expires_at,
    }
    admin_firestore().collection(ATTEMPTS_COLLECTION).document(attempt_id).create(attempt)
    return attempt_id, _authorization_url(
        connector=connector,
        state=attempt_id,
        code_challenge=challenge,
    )


def claim_attempt(attempt_id: str) -> tuple[str, dict[str, Any] | None]:
    db = admin_firestore()
    ref = db.collection(ATTEMPTS_COLLECTION).document(attempt_id)
    transaction = db.transaction()
    now = _utc_now()

    @gcloud_firestore.transactional
    def _claim(txn) -> tuple[str, dict[str, Any] | None]:
        snapshot = ref.get(transaction=txn)
        data = snapshot.to_dict() if snapshot.exists else None
        if not data:
            return "invalid", None
        status = data.get("status")
        expires_at = data.get("expires_at")
        if status == "completed":
            return "completed", data
        if status != "pending":
            return "invalid", None
        if not isinstance(expires_at, datetime):
            return "invalid", None
        aware_expiry = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        if aware_expiry <= now:
            return "expired", None
        txn.update(ref, {"status": "processing", "processing_at": now})
        return "claimed", data

    return _claim(transaction)


def finish_attempt(attempt_id: str, *, status: str, error_code: str | None = None) -> None:
    payload: dict[str, Any] = {
        "status": status,
        "completed_at": _utc_now(),
        "code_verifier": gcloud_firestore.DELETE_FIELD,
    }
    if error_code:
        payload["error_code"] = error_code
    admin_firestore().collection(ATTEMPTS_COLLECTION).document(attempt_id).update(payload)


def complete_connection(
    *,
    connector: str,
    uid: str,
    code: str,
    code_verifier: str,
    watch_url: str | None,
) -> None:
    """Dispatch a claimed attempt's auth code to the matching connector."""
    if connector == "google_calendar":
        GoogleCalendarConnector(uid).connect(
            code,
            watch_url=watch_url,
            redirect_uri=settings.GOOGLE_REDIRECT_URI,
            code_verifier=code_verifier,
        )
    elif connector == "gmail":
        GmailConnector(uid).connect(
            code,
            redirect_uri=settings.GOOGLE_REDIRECT_URI,
            code_verifier=code_verifier,
        )
    else:
        raise ValueError("invalid connector")
