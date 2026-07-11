"""
POST /events — Flutter posts user interaction events here.

Body shape:
{
  "events": [
    {
      "event_type": "notification_opened" | "notification_dismissed" |
                    "content_view" | "content_view_long" | "content_view_short" |
                    "content_liked" | "content_shared" | "content_skipped" |
                    "app_open" | "search_query",
      "content_id": "string|null",
      "category": "string|null",
      "duration_ms": int|null,
      "search_query_text": "string|null",
      "user_local_hour": int|null,
      "user_local_minute": int|null
    }
  ]
}

Each event is applied fire-and-forget through event_ingester.apply_event.
The endpoint returns 202 as soon as it has validated the payload.
"""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.request_auth import resolve_user_id_from_request
from ..services.signal_engine.event_ingester import EVENT_WEIGHTS, apply_event

KNOWN_EVENT_TYPES = set(EVENT_WEIGHTS.keys()) | {"content_view"}

MAX_EVENTS_PER_REQUEST = 25


async def handle_signal_events(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    events_raw = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events_raw, list) or not events_raw:
        return JSONResponse({"error": "Field 'events' must be a non-empty list."}, status_code=400)

    if len(events_raw) > MAX_EVENTS_PER_REQUEST:
        return JSONResponse(
            {"error": f"At most {MAX_EVENTS_PER_REQUEST} events per request."},
            status_code=400,
        )

    validated: list[dict] = []
    for event in events_raw:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "").strip()
        if event_type not in KNOWN_EVENT_TYPES:
            logger.warn("signal_events: unknown event_type dropped", {
                "user_id": user_id,
                "event_type": event_type,
            })
            continue
        validated.append({
            "event_type": event_type,
            "content_id": _coerce_optional_str(event.get("content_id")),
            "category": _coerce_optional_str(event.get("category")),
            "duration_ms": _coerce_optional_int(event.get("duration_ms")),
            "search_query_text": _coerce_optional_str(event.get("search_query_text")),
            "user_local_hour": _coerce_optional_int(event.get("user_local_hour")),
            "user_local_minute": _coerce_optional_int(event.get("user_local_minute")),
        })

    if not validated:
        return JSONResponse({"error": "No recognised events in request."}, status_code=400)

    # Fire-and-forget. Ingestion runs in the background and never blocks the
    # Flutter app waiting on a response.
    for event in validated:
        asyncio.create_task(apply_event(user_id, **event))
        # Reactive layer: the same behavioral signal updates presence (foreground +
        # dismiss-streak) for the surface-aware Delivery Arbiter. Separate task so it
        # never disturbs the signal-engine ingestion above.
        asyncio.create_task(_bridge_to_reactive(user_id, event["event_type"]))

    logger.info("signal_events: accepted", {
        "user_id": user_id,
        "accepted": len(validated),
        "received": len(events_raw),
    })
    return JSONResponse({"accepted": len(validated)}, status_code=202)


async def _bridge_to_reactive(user_id: str, event_type: str) -> None:
    """Lazy-imported wrapper so signal_events stays decoupled from the reactive
    package at module load. Never raises."""
    try:
        from ..services.reactive.behavioral_bridge import bridge_client_event

        await bridge_client_event(user_id, event_type)
    except Exception as exc:
        logger.warn("signal_events: reactive bridge failed (swallowed)", {
            "user_id": user_id, "event_type": event_type, "error": str(exc),
        })


def _coerce_optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_optional_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
