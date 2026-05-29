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
from ..services.gmail_connector import GmailConnector
from ..services.google_calendar_connector import GoogleCalendarConnector
from ..services.request_auth import resolve_user_id_from_request


class GoogleCalendarConnectBody(BaseModel):
    server_auth_code: str


class GmailConnectBody(BaseModel):
    server_auth_code: str


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

    watch_url = _resolve_watch_url(request)

    def _connect() -> dict:
        return GoogleCalendarConnector(user_id).connect(
            body.server_auth_code,
            watch_url=watch_url,
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

    def _connect() -> dict:
        return GmailConnector(user_id).connect(body.server_auth_code)

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
