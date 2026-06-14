"""GET /briefing/today

Returns the signed-in user's briefing for their current local date, if one is
ready. The server resolves the user-local date from their stored timezone, so the
client never has to compute it (avoiding a tz/date mismatch). Returns
``{"briefing": null}`` with 200 when nothing is ready yet, so the Flutter screen
shows an empty state rather than treating it as an error.

Mirrors ``handlers/signal_feed.py``: thin, auth-gated, reads the same per-user
Firestore the engine wrote.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.briefing import briefing_store as store
from ..services.briefing import world_briefing
from ..services.briefing.fields import STATUS_READY
from ..services.firebase import admin_firestore
from ..services.request_auth import resolve_user_id_from_request


def _user_local_date(user_id: str) -> str:
    """Resolve the user's current local date ("YYYY-MM-DD") from their stored
    timezone. Falls back to UTC on any miss so a date is always produced."""
    tz_name = "UTC"
    try:
        snap = admin_firestore().collection("users").document(user_id).get()
        if snap.exists:
            tz_name = str((snap.to_dict() or {}).get("timezone", "UTC") or "UTC")
    except Exception as exc:
        logger.warn("briefing: timezone fetch failed, defaulting UTC", {
            "user_id": user_id, "error": str(exc),
        })
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, Exception):
        now = datetime.now(UTC)
    return now.date().isoformat()


async def handle_get_today_briefing(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    local_date = _user_local_date(user_id)
    stored = await store.get_briefing(user_id, local_date=local_date)

    # Only a READY briefing is shown. generating/skipped/failed all read as "nothing
    # for you yet" so the client renders its empty state, never a half-written doc.
    if stored is None or stored.status != STATUS_READY:
        return JSONResponse({"briefing": None}, status_code=200)

    payload = {
        "briefing": {
            "date": stored.local_date,
            "narrative": stored.narrative,
            "chat_seed_message": stored.chat_seed_message,
            "sources": stored.sources,
        }
    }
    logger.info("briefing: served", {
        "user_id": user_id,
        "local_date": local_date,
        "sources": len(stored.sources),
    })
    return JSONResponse(payload, status_code=200)


async def handle_post_world_briefing(request: Request) -> JSONResponse:
    """POST /briefing/world — the on-demand "Catch me up on the world" snapshot.

    Generates (or serves a cached) general world catch-up (2-3 global + 1 local for the
    user's region) so the briefing screen is never a dead end for a new/cold-start user.
    Body: optional ``{"refresh": true}`` (the refresh icon) forces a regenerate, bounded
    by the per-user cooldown in :mod:`world_briefing`. Returns the same payload shape as
    ``GET /briefing/today`` (``{"briefing": {...}}`` or ``{"briefing": null}``) so the
    Flutter screen reuses the DailyBriefing model. No consent gate: this is general world
    news, not behavioural profiling.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    refresh = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            refresh = body.get("refresh", False) is True
    except Exception:
        # No / malformed body is fine — default to a normal (cache-served) open.
        refresh = False

    targeting = await store.read_user_targeting(user_id)
    result = await world_briefing.generate_world(
        user_id, timezone=targeting.timezone, force=refresh,
    )
    if result is None:
        return JSONResponse({"briefing": None}, status_code=200)

    payload = {
        "briefing": {
            "narrative": result.narrative,
            "chat_seed_message": result.chat_seed_message,
            "sources": result.sources,
            "items": result.items,
            "region": result.region_code,
        }
    }
    logger.info("briefing: world snapshot served", {
        "user_id": user_id,
        "region": result.region_code,
        "refresh": refresh,
        "sources": len(result.sources),
    })
    return JSONResponse(payload, status_code=200)
