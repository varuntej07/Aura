"""Backend-owned Google OAuth handoff for Aura Desktop connectors."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from google.cloud import firestore as gcloud_firestore
from pydantic import BaseModel, ValidationError

from ..config.settings import settings
from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.gmail_connector import GmailConnector
from ..services.google_calendar_connector import GoogleCalendarConnector
from ..services.request_auth import resolve_user_id_from_request
from .connectors import _resolve_watch_url

ATTEMPTS_COLLECTION = "connector_oauth_attempts"
ATTEMPT_TTL_SECONDS = 10 * 60
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

ConnectorName = Literal["google_calendar", "gmail"]


class ConnectorOAuthStartBody(BaseModel):
    connector: ConnectorName


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


def _completion_url(
    *,
    attempt_id: str,
    connector: str,
    outcome: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "attempt_id": attempt_id,
            "connector": connector,
            "outcome": outcome,
        }
    )
    return f"aura://connectors/complete?{query}"


def _completion_page(
    *,
    title: str,
    message: str,
    completion_url: str | None,
) -> HTMLResponse:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    open_aura = (
        f'<p><a href="{html.escape(completion_url, quote=True)}">Open Aura</a></p>'
        if completion_url
        else ""
    )
    redirect_script = (
        f"<script>window.location.replace({completion_url!r});</script>"
        if completion_url
        else ""
    )
    return HTMLResponse(
        content=(
            "<!doctype html><html><head>"
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer">'
            f"<title>{safe_title}</title>"
            "<style>"
            "body{font-family:system-ui,sans-serif;max-width:34rem;margin:15vh auto;"
            "padding:0 1.5rem;color:#272622;background:#f4eee2}"
            "a{display:inline-block;padding:.75rem 1rem;border-radius:999px;"
            "background:#1ec8b0;color:#102f2a;text-decoration:none;font-weight:700}"
            "</style></head><body>"
            f"<h1>{safe_title}</h1><p>{safe_message}</p>{open_aura}"
            f"{redirect_script}</body></html>"
        ),
        headers={"Cache-Control": "no-store"},
    )


async def start_connector_oauth(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized: valid Firebase ID token required."},
        )

    try:
        body = ConnectorOAuthStartBody.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse(status_code=400, content={"error": "invalid_connector"})

    if not (
        settings.GOOGLE_CLIENT_ID
        and settings.GOOGLE_CLIENT_SECRET
        and settings.GOOGLE_REDIRECT_URI
    ):
        return JSONResponse(status_code=503, content={"error": "google_oauth_not_configured"})

    attempt_id = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    now = _utc_now()
    expires_at = now + timedelta(seconds=ATTEMPT_TTL_SECONDS)
    attempt = {
        "uid": user_id,
        "connector": body.connector,
        "status": "pending",
        "code_verifier": verifier,
        "created_at": now,
        "expires_at": expires_at,
    }

    def _create() -> None:
        admin_firestore().collection(ATTEMPTS_COLLECTION).document(attempt_id).create(attempt)

    try:
        await asyncio.to_thread(_create)
    except Exception as exc:
        logger.exception(
            "ConnectorOAuth: attempt creation failed",
            {"user_id": user_id, "connector": body.connector, "error": str(exc)},
        )
        return JSONResponse(status_code=500, content={"error": "oauth_start_failed"})

    return JSONResponse(
        status_code=200,
        content={
            "attempt_id": attempt_id,
            "authorization_url": _authorization_url(
                connector=body.connector,
                state=attempt_id,
                code_challenge=challenge,
            ),
            "expires_in_seconds": ATTEMPT_TTL_SECONDS,
        },
        headers={"Cache-Control": "no-store"},
    )


def _claim_attempt(attempt_id: str) -> tuple[str, dict[str, Any] | None]:
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


def _finish_attempt(attempt_id: str, *, status: str, error_code: str | None = None) -> None:
    payload: dict[str, Any] = {
        "status": status,
        "completed_at": _utc_now(),
        "code_verifier": gcloud_firestore.DELETE_FIELD,
    }
    if error_code:
        payload["error_code"] = error_code
    admin_firestore().collection(ATTEMPTS_COLLECTION).document(attempt_id).update(payload)


async def complete_connector_oauth(request: Request) -> HTMLResponse:
    attempt_id = request.query_params.get("state", "")
    if len(attempt_id) != 43:
        return _completion_page(
            title="This connection expired",
            message="Return to Aura and try connecting again.",
            completion_url=None,
        )

    try:
        claim_status, attempt = await asyncio.to_thread(_claim_attempt, attempt_id)
    except Exception as exc:
        logger.exception("ConnectorOAuth: claim failed", {"error": str(exc)})
        return _completion_page(
            title="Aura could not finish connecting",
            message="Nothing changed. Return to Aura and try again.",
            completion_url=None,
        )

    if claim_status == "completed" and attempt:
        connector = str(attempt.get("connector") or "")
        return _completion_page(
            title="Connected to Aura",
            message="You can return to the Aura desktop app.",
            completion_url=_completion_url(
                attempt_id=attempt_id,
                connector=connector,
                outcome="success",
            ),
        )
    if claim_status != "claimed" or not attempt:
        return _completion_page(
            title="This connection expired",
            message="Return to Aura and try connecting again.",
            completion_url=None,
        )

    connector = str(attempt.get("connector") or "")
    outcome = "cancelled" if request.query_params.get("error") else "failed"
    code = request.query_params.get("code")
    if not code:
        await asyncio.to_thread(
            _finish_attempt,
            attempt_id,
            status="cancelled",
            error_code="consent_cancelled",
        )
        return _completion_page(
            title="Connection cancelled",
            message="Nothing changed. You can return to Aura.",
            completion_url=_completion_url(
                attempt_id=attempt_id,
                connector=connector,
                outcome=outcome,
            ),
        )

    try:
        if connector == "google_calendar":
            await asyncio.to_thread(
                GoogleCalendarConnector(str(attempt["uid"])).connect,
                code,
                watch_url=_resolve_watch_url(request),
                redirect_uri=settings.GOOGLE_REDIRECT_URI,
                code_verifier=str(attempt["code_verifier"]),
            )
        elif connector == "gmail":
            await asyncio.to_thread(
                GmailConnector(str(attempt["uid"])).connect,
                code,
                redirect_uri=settings.GOOGLE_REDIRECT_URI,
                code_verifier=str(attempt["code_verifier"]),
            )
        else:
            raise ValueError("invalid connector")
        await asyncio.to_thread(_finish_attempt, attempt_id, status="completed")
    except Exception as exc:
        logger.exception(
            "ConnectorOAuth: completion failed",
            {"connector": connector, "error": str(exc)},
        )
        await asyncio.to_thread(
            _finish_attempt,
            attempt_id,
            status="failed",
            error_code="connection_failed",
        )
        return _completion_page(
            title="Aura could not finish connecting",
            message="Nothing changed. Return to Aura and try again.",
            completion_url=_completion_url(
                attempt_id=attempt_id,
                connector=connector,
                outcome="failed",
            ),
        )

    return _completion_page(
        title="Connected to Aura",
        message="You can return to the Aura desktop app.",
        completion_url=_completion_url(
            attempt_id=attempt_id,
            connector=connector,
            outcome="success",
        ),
    )
