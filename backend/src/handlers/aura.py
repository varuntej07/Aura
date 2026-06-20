"""POST /aura/consolidate-session — the per-session reflection trigger.

The chat transcript is client-owned (the local drift DB), so when a session ends the
client ships its turns here (fired from the app-background rail, a new-session boundary,
or a resume that finds a stale un-consolidated session). We kick off the reflection tier
fire-and-forget and return immediately, so the client is never blocked. Reflection is
idempotent per session_id, GDPR-gated on consent, and swallows its own errors, so a
retry or duplicate send is safe.

Authenticated as the end user via Firebase ID token (same as /events, /threads/reply).
"""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.aura_reflection import consolidate_session
from ..services.request_auth import resolve_user_id_from_request

# The client owns the chat; this is digest input, not storage. Keep the most recent
# turns and let reflection compress further if needed.
MAX_TURNS = 400


async def handle_consolidate_session(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object."}, status_code=400)

    session_id = str(body.get("session_id", "")).strip() or None
    raw_turns = body.get("turns")
    if not isinstance(raw_turns, list):
        return JSONResponse({"error": "Field 'turns' must be a list."}, status_code=400)
    modality = str(body.get("modality", "text")).strip() or "text"

    turns = [t for t in raw_turns if isinstance(t, dict)][-MAX_TURNS:]

    # Fire-and-forget: reflection runs after the response returns. It gates on consent,
    # is idempotent per session_id, and never raises, so detaching it is safe.
    asyncio.create_task(consolidate_session(user_id, session_id, turns, modality))

    logger.info("AuraConsolidate: accepted", {
        "user_id": user_id,
        "session_id": session_id,
        "turns": len(turns),
        "modality": modality,
    })
    return JSONResponse({"status": "accepted"}, status_code=202)
