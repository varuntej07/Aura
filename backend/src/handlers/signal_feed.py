"""
GET /feed/recommend?limit=20

Returns the user's ranked feed for in-app surfaces (home screen, agent
prioritisation, suggestion pills). Reads the same signal_store state the
notification scoring loop uses, so the feed and the notifications stay
in agreement about what the user cares about.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.request_auth import resolve_user_id_from_request
from ..services.signal_engine.recommender import DEFAULT_FEED_LIMIT, rank_session


async def handle_signal_feed(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        limit = int(request.query_params.get("limit") or DEFAULT_FEED_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_FEED_LIMIT

    items = await rank_session(user_id, limit=limit)
    payload = {"items": [asdict(item) for item in items]}

    logger.info("signal_feed: served", {
        "user_id": user_id,
        "item_count": len(items),
        "limit": limit,
    })
    return JSONResponse(payload, status_code=200)
