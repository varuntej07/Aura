"""Explicit Meeting V2 metadata backfill.

This is intentionally not scheduler-wired. It may be invoked manually after
rollout review and only labels legacy rows; it never fabricates a digest,
generation, receipt, run identity, fence, or artifact pointer.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F


async def label_known_legacy_meetings(*, limit: int = 500) -> dict[str, int]:
    def _run() -> dict[str, int]:
        db = admin_firestore()
        scanned = 0
        labeled = 0
        for user in db.collection(F.PARENT_COLLECTION).limit(limit).stream():
            for meeting in (
                user.reference.collection(F.SUBCOLLECTION).limit(max(1, limit - scanned)).stream()
            ):
                scanned += 1
                data = meeting.to_dict() or {}
                if F.PROTOCOL_VERSION in data:
                    continue
                # Absence of V2 identity is the only fact being recorded.
                # Legacy segment arrays are deliberately not converted.
                meeting.reference.update(
                    {
                        F.PROTOCOL_VERSION: 1,
                        "backfill_status": "legacy_unverified",
                        "backfilled_at": datetime.now(UTC).isoformat(),
                    }
                )
                labeled += 1
                if scanned >= limit:
                    break
            if scanned >= limit:
                break
        return {"scanned": scanned, "labeled": labeled}

    result = await asyncio.to_thread(_run)
    logger.info("meetings.backfill: legacy metadata labeled", result)
    return result
