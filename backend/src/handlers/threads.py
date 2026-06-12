"""POST /threads/reply — the user answered a curiosity follow-up in the shade.

This is the silent inline-reply path: the user picked a suggestion chip (or
typed) inside the notification without opening the app. We persist the exchange
to the thread's server-authoritative conversation, mark the thread engaged,
enrich the UserAura profile from what they shared, and synchronously return
Buddy's short reply so the client can update the notification shade with it.

Authenticated as the end user via Firebase ID token (same as /events), never the
scheduler service account.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.analytics import posthog_client
from ..services.analytics.funnel_events import (
    EVENT_THREAD_REPLY,
    NOTIFICATION_ORIGIN_THREAD_ENGINE,
    PROP_NOTIFICATION_ORIGIN,
    PROP_THREAD_ID,
)
from ..services.model_provider import get_model_provider
from ..services.request_auth import resolve_user_id_from_request
from ..services.threads import thread_store
from ..services.threads.models import Thread, ThreadSource, ThreadStatus
from ..services.threads.thread_responder import generate_thread_reply
from ..services.user_aura_extractor import extract_and_update_user_aura

MAX_REPLY_CHARS = 2_000


async def handle_thread_reply(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object."}, status_code=400)

    thread_id = str(body.get("thread_id", "")).strip()
    reply = str(body.get("reply", "")).strip()
    question = str(body.get("question", "")).strip()

    if not thread_id:
        return JSONResponse({"error": "Field 'thread_id' is required."}, status_code=400)
    if not reply:
        return JSONResponse({"error": "Field 'reply' is required."}, status_code=400)
    if len(reply) > MAX_REPLY_CHARS:
        return JSONResponse(
            {"error": f"Reply must be {MAX_REPLY_CHARS} characters or fewer."},
            status_code=400,
        )

    now = datetime.now(UTC)

    # Load the thread for the responder's context. A missing thread is tolerated
    # (the loop may have been pruned) — we still answer the user rather than drop
    # their message, using a minimal stand-in built from the question.
    thread = await thread_store.get_thread(user_id, thread_id)
    if thread is None:
        thread = Thread(
            thread_id=thread_id,
            trigger_text=question or reply,
            source=ThreadSource.CHAT,
        )

    # Persist the user's answer + flip the loop to engaged before the LLM call,
    # so the exchange survives even if reply generation later fails.
    await thread_store.append_message(
        user_id, thread_id, role="user", content=reply, created_at=now,
    )
    await thread_store.set_status(user_id, thread_id, ThreadStatus.ENGAGED)

    # Enrich the aura from what they shared, exactly like a normal chat turn:
    # the question is the prior Buddy utterance, the reply is the user message.
    asyncio.create_task(
        extract_and_update_user_aura(user_id, reply, thread_id, question or None)
    )

    models = get_model_provider()
    buddy_reply = await generate_thread_reply(
        models, thread, question=question, user_reply=reply,
    )
    await thread_store.append_message(
        user_id, thread_id, role="assistant",
        content=buddy_reply, created_at=datetime.now(UTC),
    )

    # Funnel action step: a shade reply is a conversion even though the app
    # never opened (so no tap/session event fires for this path). Fire server-
    # side and flush before the response, since the container may freeze after.
    await posthog_client.capture_event(
        distinct_id=user_id,
        event=EVENT_THREAD_REPLY,
        properties={
            PROP_THREAD_ID: thread_id,
            PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_THREAD_ENGINE,
            "channel": "shade",
        },
    )
    await posthog_client.flush()

    logger.info("threads: shade reply handled", {
        "user_id": user_id,
        "thread_id": thread_id,
        "reply_len": len(reply),
    })
    return JSONResponse({"reply": buddy_reply}, status_code=200)


async def handle_thread_messages(request: Request, thread_id: str) -> JSONResponse:
    """GET the server-authoritative conversation for a thread.

    Lets the client reconcile a shade exchange (the user replied inside the
    notification, app never opened) into its chat view when the thread is later
    opened. Authenticated as the end user.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    thread_id = (thread_id or "").strip()
    if not thread_id:
        return JSONResponse({"error": "thread_id is required."}, status_code=400)

    messages = await thread_store.list_messages(user_id, thread_id)
    return JSONResponse({"messages": messages}, status_code=200)
