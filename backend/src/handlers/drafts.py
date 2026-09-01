"""GET/DELETE /drafts - the dashboard's "Drafts" feed.

Same shape as handlers/screen_saves.py: read-only list + per-item hard delete,
auth via the same Firebase ID token check, not consent-gated for the same
reason - a user can always see and delete their own stored data. Writes happen
ONLY from the voice worker (agent/voice/draft_outbound.py) and the refine
endpoint's update-only leg (handlers/draft_outbound.py); this module never
writes a draft, only reads and deletes.

Simpler than screen_saves because a draft has no GCS image leg; the doc is
pure text. ``session_id`` stays server-side (provenance only), matching how
screen_saves omits its internals from the list response.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.drafts import store
from .request_guards import require_user


async def handle_list_drafts(request: Request) -> JSONResponse:
    """GET /drafts - recent drafts, newest first, already filtered of expired
    rows. Fails closed (empty list) rather than raising, matching
    screen_saves' read path."""
    user_id = require_user(request)

    items = await store.list_drafts(user_id)
    logger.info("Drafts: listed", {"user_id": user_id, "total": len(items)})
    return JSONResponse({"items": items})


async def handle_delete_draft(request: Request, draft_id: str) -> JSONResponse:
    """DELETE /drafts/{draft_id} - forget one draft. Always allowed for the
    owner. Hard delete, no tombstone; the store's update-only refine paths
    guarantee a deleted draft can never come back."""
    user_id = require_user(request)
    draft_id = (draft_id or "").strip()
    if not draft_id:
        return JSONResponse({"error": "Missing draft id."}, status_code=400)

    ok = await store.delete_draft(user_id, draft_id)
    logger.info("Drafts: deleted", {"user_id": user_id, "draft_id": draft_id, "ok": ok})
    return JSONResponse({"ok": ok})
