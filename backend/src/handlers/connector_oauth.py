"""Backend-owned Google OAuth handoff for Aura Desktop connectors."""

from __future__ import annotations

import asyncio
import html
import urllib.parse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from ..config.settings import settings
from ..lib.logger import logger
from ..services.connector_oauth import (
    ATTEMPT_ID_LENGTH,
    ATTEMPT_TTL_SECONDS,
    ConnectorName,
    claim_attempt,
    complete_connection,
    create_attempt,
    finish_attempt,
    resolve_watch_url,
)
from ..services.request_auth import resolve_user_id_from_request


class ConnectorOAuthStartBody(BaseModel):
    connector: ConnectorName


def _watch_url_from_request(request: Request) -> str | None:
    return resolve_watch_url(
        proto=request.headers.get("x-forwarded-proto") or request.url.scheme,
        host=request.headers.get("x-forwarded-host") or request.headers.get("host"),
    )


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

    def _create() -> tuple[str, str]:
        return create_attempt(user_id=user_id, connector=body.connector)

    try:
        attempt_id, authorization_url = await asyncio.to_thread(_create)
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
            "authorization_url": authorization_url,
            "expires_in_seconds": ATTEMPT_TTL_SECONDS,
        },
        headers={"Cache-Control": "no-store"},
    )


async def complete_connector_oauth(request: Request) -> HTMLResponse:
    attempt_id = request.query_params.get("state", "")
    if len(attempt_id) != ATTEMPT_ID_LENGTH:
        return _completion_page(
            title="This connection expired",
            message="Return to Aura and try connecting again.",
            completion_url=None,
        )

    try:
        claim_status, attempt = await asyncio.to_thread(claim_attempt, attempt_id)
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
            finish_attempt,
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
        await asyncio.to_thread(
            complete_connection,
            connector=connector,
            uid=str(attempt["uid"]),
            code=code,
            code_verifier=str(attempt["code_verifier"]),
            watch_url=_watch_url_from_request(request),
        )
        await asyncio.to_thread(finish_attempt, attempt_id, status="completed")
    except Exception as exc:
        logger.exception(
            "ConnectorOAuth: completion failed",
            {"connector": connector, "error": str(exc)},
        )
        await asyncio.to_thread(
            finish_attempt,
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
