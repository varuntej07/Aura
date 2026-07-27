from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from google.api_core.exceptions import AlreadyExists

from ...lib.logger import logger
from ..firebase import admin_firestore
from .models import GetBetterActivityBatch

ACTIVITY_SUBCOLLECTION = "get_better_activity"
ACTIVITY_RETENTION_DAYS = 180
ACTIVITY_SCHEMA_VERSION = 1


async def store_activity_batch(user_id: str, batch: GetBetterActivityBatch) -> bool:
    """Persist one idempotent batch document.

    The batch id is supplied by the durable client outbox. Firestore ``create``
    gives retries idempotency without a preliminary read or a transaction, so a
    successful flush costs exactly one document write.
    """

    def _write() -> bool:
        document_ref = (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection(ACTIVITY_SUBCOLLECTION)
            .document(batch.batch_id)
        )
        now = datetime.now(UTC)
        payload = {
            "schema_version": ACTIVITY_SCHEMA_VERSION,
            "created_at": now,
            "expires_at": now + timedelta(days=ACTIVITY_RETENTION_DAYS),
            "event_count": len(batch.events),
            "events": [
                event.model_dump(mode="json")
                for event in sorted(batch.events, key=lambda event: event.occurred_at)
            ],
        }
        try:
            document_ref.create(payload)
            return True
        except AlreadyExists:
            return False

    created = await asyncio.to_thread(_write)
    logger.info(
        "get_better: activity batch accepted",
        {
            "batch_id": batch.batch_id,
            "events": len(batch.events),
            "created": created,
        },
    )
    return created
