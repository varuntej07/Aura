"""GET/DELETE /screen-saves — the desktop "Screen Saves" dashboard.

Same shape as handlers/history.py: read-only list + per-item hard delete,
auth via the same Firebase ID token check, not consent-gated for the same
reason handle_get_memory isn't — a user can always see and delete their own
stored data. Writes happen ONLY from the deterministic voice action
(agent/voice/screen_saves.py); this module never writes a screen_saves item,
only reads and deletes.

The list response mints one short-lived v4 signed URL per item that has an
image (services/gcs.py) so the dashboard never needs direct bucket access.
"""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services import gcs
from ..services.screen_saves import fields as F
from .request_guards import require_user
from ..services.screen_saves import store


async def _with_signed_url(item: dict) -> dict:
    image_path = item.get(F.IMAGE_PATH)
    image_url = await gcs.signed_url_for(image_path) if image_path else None
    return {**item, "image_url": image_url}


async def handle_list_screen_saves(request: Request) -> JSONResponse:
    """GET /screen-saves — recent screen saves, newest first. Fails closed
    (empty list) rather than raising, matching history.py's read paths."""
    user_id = require_user(request)

    try:
        items = await store.list_items(user_id)
        items_with_urls = await asyncio.gather(*(_with_signed_url(item) for item in items))
    except Exception as exc:
        logger.warn("ScreenSaves: list failed", {"user_id": user_id, "error": str(exc)})
        return JSONResponse({"items": []})

    logger.info("ScreenSaves: listed", {"user_id": user_id, "total": len(items_with_urls)})
    return JSONResponse({"items": list(items_with_urls)})


async def handle_delete_screen_save(request: Request, item_id: str) -> JSONResponse:
    """DELETE /screen-saves/{item_id} — forget one screen save. Always allowed
    for the owner. Hard delete, no tombstone, matching history.py's own
    delete semantics. Bytes first, pointer second: the GCS object is removed
    BEFORE the Firestore doc, because the doc carries the only reference to
    the object — deleting the doc first would orphan the user's screen image
    in the bucket with nothing left that can find it. A failed object delete
    returns 503 so the client retries the whole delete."""
    user_id = require_user(request)
    item_id = (item_id or "").strip()
    if not item_id:
        return JSONResponse({"error": "Missing item id."}, status_code=400)

    try:
        item = await store.get_item_strict(user_id, item_id)
    except Exception as exc:
        logger.warn(
            "ScreenSaves: delete lookup failed",
            {"user_id": user_id, "item_id": item_id, "error": str(exc)},
        )
        return JSONResponse({"error": "Deletion temporarily unavailable."}, status_code=503)

    image_path = (item or {}).get(F.IMAGE_PATH)
    if image_path and not await gcs.delete_screen_save(image_path):
        return JSONResponse({"error": "Deletion temporarily unavailable."}, status_code=503)

    ok = await store.delete_item(user_id, item_id)
    logger.info("ScreenSaves: deleted", {"user_id": user_id, "item_id": item_id, "ok": ok})
    return JSONResponse({"ok": ok})
