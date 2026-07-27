from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..services.get_better.activity_store import store_activity_batch
from ..services.get_better.get_better_service import get_feed
from ..services.get_better.models import GetBetterActivityBatch
from ..services.request_auth import resolve_user_id_from_request


async def handle_post_get_better_ideas(request: Request) -> JSONResponse:
    """Serve the original direct-feed shape for installed client compatibility."""

    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    feed = await get_feed()
    return JSONResponse({"feed": feed.model_dump(mode="json")}, status_code=200)


async def handle_post_get_better_catalog(request: Request) -> JSONResponse:
    """Version-aware catalog sync for current clients."""

    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    known_catalog_version: str | None = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            raw_version = body.get("known_catalog_version")
            if isinstance(raw_version, str) and 0 < len(raw_version) <= 64:
                known_catalog_version = raw_version
    except Exception:
        pass

    feed = await get_feed()
    if known_catalog_version == feed.catalog_version:
        return JSONResponse(
            {
                "not_modified": True,
                "catalog_version": feed.catalog_version,
            },
            status_code=200,
        )
    return JSONResponse(
        {
            "not_modified": False,
            "catalog_version": feed.catalog_version,
            "feed": feed.model_dump(mode="json"),
        },
        status_code=200,
    )


async def handle_post_get_better_activity(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        batch = GetBetterActivityBatch.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError) as exc:
        return JSONResponse(
            {"error": "Invalid Get Better activity batch", "detail": str(exc)},
            status_code=400,
        )

    created = await store_activity_batch(user_id, batch)
    return JSONResponse(
        {
            "accepted": len(batch.events),
            "deduplicated": not created,
        },
        status_code=200,
    )
