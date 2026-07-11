"""The per-user event inbox the orchestrator drains.

``drain`` reads a user's unconsumed outbox events (a single equality filter at
collection scope, auto-indexed — no composite declaration needed; ordering is done
in Python to stay index-free, matching ``notification_queue``). ``mark_consumed``
flips them after the orchestrator has dispatched. Coalescing lives here: the
orchestrator drains the whole inbox in ONE pass per invocation, so a burst of
events becomes one reconcile + one decide, not one orchestrate per event.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore as fs  # type: ignore

from ...lib.logger import logger
from ..firebase import admin_firestore
from .events import Event
from .fields import (
    FIELD_CONSUMED,
    FIELD_CONSUMED_AT,
    OUTBOX_SUBCOLLECTION,
    USERS_COLLECTION,
)

# A user accumulates few events between drains; cap the read so a runaway producer
# can never pull an unbounded page into memory.
MAX_DRAIN = 100


def _outbox_col(user_id: str):
    return (
        admin_firestore()
        .collection(USERS_COLLECTION)
        .document(user_id)
        .collection(OUTBOX_SUBCOLLECTION)
    )


async def drain(user_id: str, *, limit: int = MAX_DRAIN) -> list[tuple[Any, Event]]:
    """All unconsumed events for a user, oldest first. Returns ``(ref, Event)``
    pairs so the caller can mark them consumed after dispatch. Degrades to ``[]``
    on a read failure (the events stay unconsumed and the next sweep retries)."""

    def _fetch() -> list[tuple[Any, Event]]:
        snaps = (
            _outbox_col(user_id)
            .where(filter=fs.FieldFilter(FIELD_CONSUMED, "==", False))
            .limit(limit)
            .stream()
        )
        out: list[tuple[Any, Event]] = []
        for snap in snaps:
            out.append((snap.reference, Event.from_dict(snap.to_dict() or {})))
        out.sort(key=lambda pair: pair[1].ts)
        return out

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("inbox.drain failed", {"user_id": user_id, "error": str(exc)})
        return []


async def mark_consumed(user_id: str, refs: list[Any], *, now: datetime | None = None) -> None:
    """Flip drained events to consumed. Best-effort: a failure here means the next
    sweep re-drains them, and the orchestrator's idempotent per-event consume makes
    that a no-op (no double dispatch)."""
    if not refs:
        return
    when = now or datetime.now(UTC)

    def _write() -> None:
        batch = admin_firestore().batch()
        for ref in refs:
            batch.update(ref, {FIELD_CONSUMED: True, FIELD_CONSUMED_AT: when})
        batch.commit()

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("inbox.mark_consumed failed", {"user_id": user_id, "error": str(exc)})
