"""
Central query logger: Writes every user input to users/{uid}/queries/{id}.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from .logger import logger

QueryType = Literal["chat", "voice"]


async def log_query(
    user_id: str,
    query_type: QueryType,
    text: str,
    session_id: str | None = None,
    client_message_id: str | None = None,
) -> None:
    """Idempotent write to users/{uid}/queries/{id}"""
    try:
        from ..services.firebase import admin_firestore
        db = admin_firestore()
        query_id = client_message_id or str(uuid.uuid4())
        doc: dict = {
            "text": text,
            "type": query_type,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if session_id:
            doc["session_id"] = session_id
        db.collection("users").document(user_id).collection("queries").document(query_id).set(doc)

        # Update last_app_interaction_at in engagement_guard so the decision engine
        # can detect whether the user is currently active and suppress proactive pings
        db.collection("users").document(user_id)\
            .collection("engagement_guard").document("state")\
            .set({"last_app_interaction_at": doc["timestamp"]}, merge=True)
    except Exception as exc:
        logger.warn("query_logger: write failed", {"user_id": user_id, "error": str(exc)})
