from __future__ import annotations

import re

from fastapi import Request
from fastapi.responses import JSONResponse

from ..services.get_better.get_better_service import generate_feed
from ..services.request_auth import resolve_user_id_from_request


async def handle_post_get_better_ideas(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    cursor = 0
    excluded_ids: list[str] = []
    try:
        body = await request.json()
        if isinstance(body, dict):
            raw_cursor = body.get("cursor", 0)
            if isinstance(raw_cursor, int) and not isinstance(raw_cursor, bool):
                cursor = max(0, min(raw_cursor, 1_000))
            raw_excluded = body.get("exclude_ids")
            if isinstance(raw_excluded, list):
                excluded_ids = [
                    candidate
                    for value in raw_excluded[:30]
                    if (candidate := str(value).strip())
                    and re.fullmatch(r"[a-z0-9_]{3,72}", candidate)
                ]
    except Exception:
        pass

    feed = await generate_feed(
        user_id,
        cursor=cursor,
        excluded_ids=excluded_ids,
    )
    return JSONResponse({"feed": feed.model_dump(mode="json")}, status_code=200)
