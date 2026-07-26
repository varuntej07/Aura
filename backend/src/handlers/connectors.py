"""
Connector REST handlers for Google Calendar.
"""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from ..config.settings import settings
from ..lib.logger import logger
from ..services.gmail_connector import GmailConnector, GmailReauthorizationRequired
from ..services.google_calendar_connector import (
    GoogleCalendarConnector,
    GoogleCalendarReauthorizationRequired,
)
from ..services.request_auth import resolve_user_id_from_request


class GoogleCalendarConnectBody(BaseModel):
    server_auth_code: str
    redirect_uri: str | None = None


class GmailConnectBody(BaseModel):
    server_auth_code: str
    redirect_uri: str | None = None


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": "Unauthorized: valid Firebase ID token required."},
    )


def _resolve_watch_url(request: Request) -> str | None:
    if settings.GOOGLE_CALENDAR_WEBHOOK_URL:
        return settings.GOOGLE_CALENDAR_WEBHOOK_URL

    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto == "https" and host:
        return f"https://{host}/integrations/google-calendar/webhook"

    return None


def _validate_web_oauth_request(request: Request, redirect_uri: str | None) -> str | None:
    """Validate the popup code flow without changing native mobile callers."""
    if not redirect_uri:
        return None

    allowed_redirects = {origin.rstrip("/") for origin in settings.cors_allowed_origins}
    if redirect_uri not in allowed_redirects:
        return "redirect_uri is not allowed."

    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin != redirect_uri:
        return "OAuth request origin does not match redirect_uri."

    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        return "X-Requested-With is required for web OAuth."

    return None


async def get_connectors(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    def _load() -> dict:
        return {
            "google_calendar": GoogleCalendarConnector(user_id).get_status(),
            "gmail": GmailConnector(user_id).get_status(),
        }

    catalog = await asyncio.to_thread(_load)
    return JSONResponse(status_code=200, content=catalog)


async def connect_google_calendar(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    try:
        body = GoogleCalendarConnectBody.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse(status_code=400, content={"error": "server_auth_code is required."})

    redirect_uri = body.redirect_uri.rstrip("/") if body.redirect_uri else None
    validation_error = _validate_web_oauth_request(request, redirect_uri)
    if validation_error:
        return JSONResponse(status_code=400, content={"error": validation_error})

    watch_url = _resolve_watch_url(request)

    def _connect() -> dict:
        return GoogleCalendarConnector(user_id).connect(
            body.server_auth_code,
            watch_url=watch_url,
            redirect_uri=redirect_uri,
        )

    try:
        status = await asyncio.to_thread(_connect)
        return JSONResponse(status_code=200, content=status)
    except Exception as exc:
        logger.exception("Google Calendar connect failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse(status_code=500, content={"error": str(exc)})


async def disconnect_google_calendar(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    def _disconnect() -> dict:
        return GoogleCalendarConnector(user_id).disconnect()

    try:
        status = await asyncio.to_thread(_disconnect)
        return JSONResponse(status_code=200, content=status)
    except Exception as exc:
        logger.exception("Google Calendar disconnect failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse(status_code=500, content={"error": str(exc)})


async def enable_google_calendar(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    watch_url = _resolve_watch_url(request)

    def _enable() -> dict:
        return GoogleCalendarConnector(user_id).enable(watch_url=watch_url)

    try:
        status = await asyncio.to_thread(_enable)
        return JSONResponse(status_code=200, content=status)
    except GoogleCalendarReauthorizationRequired:
        return JSONResponse(
            status_code=409,
            content={"error": "reauthorization_required"},
        )
    except Exception as exc:
        logger.exception(
            "Google Calendar enable failed",
            {"user_id": user_id, "error": str(exc)},
        )
        return JSONResponse(status_code=500, content={"error": "enable_failed"})


async def disable_google_calendar(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    def _disable() -> dict:
        return GoogleCalendarConnector(user_id).disable()

    try:
        status = await asyncio.to_thread(_disable)
        return JSONResponse(status_code=200, content=status)
    except Exception as exc:
        logger.exception(
            "Google Calendar disable failed",
            {"user_id": user_id, "error": str(exc)},
        )
        return JSONResponse(status_code=500, content={"error": "disable_failed"})


async def sync_google_calendar(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    def _sync() -> dict:
        return GoogleCalendarConnector(user_id).sync_now()

    try:
        status = await asyncio.to_thread(_sync)
        return JSONResponse(status_code=200, content=status)
    except Exception as exc:
        logger.exception("Google Calendar sync failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse(status_code=500, content={"error": str(exc)})


async def connect_gmail(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    try:
        body = GmailConnectBody.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse(status_code=400, content={"error": "server_auth_code is required."})

    redirect_uri = body.redirect_uri.rstrip("/") if body.redirect_uri else None
    validation_error = _validate_web_oauth_request(request, redirect_uri)
    if validation_error:
        return JSONResponse(status_code=400, content={"error": validation_error})

    def _connect() -> dict:
        return GmailConnector(user_id).connect(
            body.server_auth_code,
            redirect_uri=redirect_uri,
        )

    try:
        status = await asyncio.to_thread(_connect)
        return JSONResponse(status_code=200, content=status)
    except Exception as exc:
        logger.exception("Gmail connect failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse(status_code=500, content={"error": str(exc)})


async def disconnect_gmail(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    def _disconnect() -> dict:
        return GmailConnector(user_id).disconnect()

    try:
        status = await asyncio.to_thread(_disconnect)
        return JSONResponse(status_code=200, content=status)
    except Exception as exc:
        logger.exception("Gmail disconnect failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse(status_code=500, content={"error": str(exc)})


async def enable_gmail(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    try:
        status = await asyncio.to_thread(GmailConnector(user_id).enable)
        return JSONResponse(status_code=200, content=status)
    except GmailReauthorizationRequired:
        return JSONResponse(
            status_code=409,
            content={"error": "reauthorization_required"},
        )
    except Exception as exc:
        logger.exception("Gmail enable failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse(status_code=500, content={"error": "enable_failed"})


async def disable_gmail(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    try:
        status = await asyncio.to_thread(GmailConnector(user_id).disable)
        return JSONResponse(status_code=200, content=status)
    except Exception as exc:
        logger.exception("Gmail disable failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse(status_code=500, content={"error": "disable_failed"})


async def google_calendar_webhook(request: Request) -> JSONResponse:
    headers = {k.lower(): v for k, v in request.headers.items()}
    channel_id = headers.get("x-goog-channel-id", "")

    connector = GoogleCalendarConnector.for_channel_id(channel_id)
    if connector is None:
        return JSONResponse(status_code=404, content={"error": "Unknown Google Calendar channel."})

    try:
        connector.enqueue_sync_from_notification(headers)
        return JSONResponse(status_code=202, content={"ok": True})
    except Exception as exc:
        logger.warn("Google Calendar webhook rejected", {
            "channel_id": channel_id,
            "error": str(exc),
        })
        return JSONResponse(status_code=400, content={"error": str(exc)})
