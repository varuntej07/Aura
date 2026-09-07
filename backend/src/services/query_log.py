"""
Central query logger: Writes every user input to users/{uid}/queries/{id}.

Side effect: each write also stamps engagement_guard/state.last_app_interaction_at,
so the decision engine can tell the user is currently active and suppress
proactive pings.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Literal

from ..lib.logger import logger

QueryType = Literal["chat", "voice"]


async def log_query(
    user_id: str,
    query_type: QueryType,
    text: str,
    session_id: str | None = None,
    client_message_id: str | None = None,
) -> None:
    """Idempotent write to users/{uid}/queries/{id}

    Both writes run in a worker thread. The firebase_admin Firestore client is
    SYNCHRONOUS, so calling .set() straight from this coroutine blocked the event
    loop for two gRPC round trips -- and because chat fires this via
    asyncio.create_task, that stall landed on the SSE generator for the current turn
    AND every other user's stream sharing the loop. Every sibling background task on
    the chat path (user_aura_extractor, mobile_compaction, session lifecycle,
    turn_store) already uses to_thread; this was the one that did not.
    """
    try:
        from .firebase import admin_firestore
        db = admin_firestore()
        query_id = client_message_id or str(uuid.uuid4())
        doc: dict = {
            "text": text,
            "type": query_type,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if session_id:
            doc["session_id"] = session_id

        def _write() -> None:
            db.collection("users").document(user_id)\
                .collection("queries").document(query_id).set(doc)

            # Update last_app_interaction_at in engagement_guard so the decision engine
            # can detect whether the user is currently active and suppress proactive pings
            db.collection("users").document(user_id)\
                .collection("engagement_guard").document("state")\
                .set({"last_app_interaction_at": doc["timestamp"]}, merge=True)

        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("query_logger: write failed", {"user_id": user_id, "error": str(exc)})
