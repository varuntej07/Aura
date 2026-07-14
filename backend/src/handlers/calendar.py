"""
Read-only calendar REST handlers.

Backs the desktop/client agenda surfaces (GET /calendar/upcoming). Connect /
disconnect / sync of the underlying Google Calendar integration live in
connectors.py; this module only reads the already-synced events.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.google_calendar_connector import GoogleCalendarConnector
from ..services.request_auth import resolve_user_id_from_request

# Mirrors the desktop client's UPCOMING_LIMIT; caps a pathologically busy day.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": "Unauthorized: valid Firebase ID token required."},
    )


def _parse_limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(value, _MAX_LIMIT))


def _is_over(event: dict, now: datetime) -> bool:
    """True once an event has already ended, so the agenda only lists what's
    left of the day. Falls back to the start time when there's no end, and keeps
    anything unparseable rather than hiding it."""
    stamp = event.get("end_time") or event.get("start_time")
    if not isinstance(stamp, str) or not stamp:
        return False
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment < now


async def get_upcoming_calendar(request: Request) -> JSONResponse:
    """GET /calendar/upcoming?limit=N -> { connected, events }.

    `connected` is the client-facing name for the connector's `configured`
    (an enabled Google Calendar integration exists). Not-connected is a normal
    200 with `connected: false`, never an error, so the client can distinguish
    it from a transport failure.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return _unauthorized()

    limit = _parse_limit(request)

    def _query() -> dict:
        # "this_week" spans from today's local midnight through the next 7 days,
        # so the agenda can fall back to the next upcoming event when today is
        # empty (and only show its "no events / create one" state when the whole
        # week is clear). Starting at local midnight (not "now") keeps a meeting
        # that's already in progress; the _is_over filter below then trims the
        # ones that have fully ended.
        return GoogleCalendarConnector(user_id).query_events(
            range_name="this_week",
            start_time=None,
            end_time=None,
            limit=limit,
        )

    try:
        result = await asyncio.to_thread(_query)
    except Exception as exc:
        logger.exception("Calendar upcoming query failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse(status_code=500, content={"error": str(exc)})

    now = datetime.now(UTC)
    events = [ev for ev in result.get("events", []) if not _is_over(ev, now)]

    return JSONResponse(
        status_code=200,
        content={
            "connected": bool(result.get("configured")),
            "events": events,
        },
    )
