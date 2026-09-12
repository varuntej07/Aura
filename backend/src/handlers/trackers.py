"""Create a topic tracker straight from a tapped home-deck card.

Tracker provisioning has only ever been reachable through the LLM tool layer. A
card needs it reachable under a finger, and needs it to return immediately: the
inline research pass that text chat runs takes seconds, which is fine inside a
turn the user is already waiting on and wrong for a button. So this takes the
same non-blocking path voice already uses, where the topic is created minimally
and the every-15-minute reconcile fills in the schedule.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.request_auth import resolve_user_id_from_request
from ..services.tracking.tracking_engine import provision_tracker


async def handle_create_tracker(request: Request) -> JSONResponse:
    """POST /trackers — subscribe the user to a topic Buddy will follow."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid request."}, status_code=400)

    topic = " ".join(str(body.get("request") or "").strip().split())[:300]
    if not topic:
        return JSONResponse({"error": "Invalid request."}, status_code=400)

    try:
        result = await provision_tracker(
            user_id,
            topic,
            created_via="deck",
            inline_research=False,
        )
    except Exception as exc:
        logger.error("trackers: deck create failed", {
            "user_id": user_id, "error": str(exc),
        })
        return JSONResponse({"error": "Temporarily unavailable"}, status_code=503)

    if not result.get("ok"):
        return JSONResponse(
            {"error": result.get("message", "I couldn't start that one.")},
            status_code=400,
        )
    return JSONResponse(
        {
            "tracker_id": result.get("tracker_id", ""),
            "title": result.get("title", topic),
            # provision_tracker is idempotent per topic, so a second tap reports the
            # subscription that already exists rather than creating a rival one.
            "created": not bool(result.get("already")),
        },
        status_code=201,
    )
