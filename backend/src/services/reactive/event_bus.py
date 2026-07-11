"""The transactional outbox + the relay that dispatches it.

Cloud Run scales to zero and can reap an instance the moment it returns an HTTP
response, before any background ``await`` runs, so an in-process
``asyncio.create_task`` dispatch is a lost-event risk. The fix is the
transactional outbox: a producer writes its business state AND the event doc in
ONE Firestore batch/transaction (``stage_event``), so the event is durable iff
the business write committed.

One flag drives everything: ``consumed``. An event is written ``consumed=false``.
The relay (``dispatch_pending``, on the per-minute ``/scheduler/tick``) finds users
with any unconsumed event and enqueues ONE coalesced ``/internal/orchestrate``
Cloud Task per user. The orchestrator drains the user's unconsumed events,
dispatches agents, and marks them ``consumed=true``. If an orchestrate task is
lost, the events stay ``consumed=false`` and the next sweep re-enqueues — at-least-
once with no "published but stranded" gap. Duplicate orchestrates are absorbed by
the single-flight lease + the idempotent per-event consume in the orchestrator.

Dispatch is hybrid (§4.1): presence events also get a best-effort post-commit
inline enqueue (``dispatch_inline``) for ~1s latency, with the sweep as the
backstop; domain/temporal events ride the ≤60s sweep alone.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore as fs  # type: ignore

from ...lib.logger import logger
from ..engagement.task_scheduler import get_task_scheduler
from ..firebase import admin_firestore
from .events import Event, is_known_event_type
from .fields import (
    FIELD_CONSUMED,
    FIELD_EXPIRES_AT,
    FIELD_TS,
    FIELD_UID,
    OUTBOX_SUBCOLLECTION,
    OUTBOX_TTL,
    USERS_COLLECTION,
)

# Bound every sweep (CLAUDE.md: sweeps are limited + cursored so a backlog can
# never burst the per-minute tick). 200 events/minute is far above the real rate
# for the tester cohort; a deeper backlog simply drains over successive ticks.
DISPATCH_BATCH_LIMIT = 200


# ── Building + staging events ────────────────────────────────────────────────
def build_event(
    uid: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    source: str = "",
    dedup_id: str = "",
    ts: datetime | None = None,
) -> Event:
    """Validate + construct an Event. Raises ``ValueError`` on an unknown type or
    a missing uid (the bus stays a thin, debuggable transport — a typo'd event
    type fails loudly at the producer, never travels)."""
    if not uid:
        raise ValueError("event requires a uid")
    if not is_known_event_type(event_type):
        raise ValueError(f"unknown event type: {event_type!r}")
    return Event(
        uid=uid,
        type=event_type,
        payload=dict(payload or {}),
        source=source,
        dedup_id=dedup_id,
        ts=ts or datetime.now(UTC),
    )


def _outbox_ref(uid: str, event_id: str):
    return (
        admin_firestore()
        .collection(USERS_COLLECTION)
        .document(uid)
        .collection(OUTBOX_SUBCOLLECTION)
        .document(event_id)
    )


def _outbox_doc(event: Event) -> dict[str, Any]:
    doc = event.to_dict()
    doc[FIELD_CONSUMED] = False
    doc[FIELD_EXPIRES_AT] = event.ts + OUTBOX_TTL
    return doc


def stage_event(writer: Any, event: Event) -> None:
    """Add the event's outbox write to a caller's Firestore batch or transaction,
    so business state + event commit atomically. ``writer`` is anything with a
    ``.set(reference, data)`` (a ``WriteBatch`` or a ``Transaction``)."""
    writer.set(_outbox_ref(event.uid, event.event_id), _outbox_doc(event))


async def emit(
    uid: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    source: str = "",
    dedup_id: str = "",
    now: datetime | None = None,
) -> str:
    """Commit a standalone event (no business state to co-commit, e.g. a presence
    event at the ``/events`` edge, or a periodic ``tick``). Returns the
    ``event_id``. For a domain event that mutates state, use ``stage_event`` inside
    the producer's own batch so the two commit atomically."""
    event = build_event(uid, event_type, payload, source=source, dedup_id=dedup_id, ts=now)

    def _commit() -> None:
        batch = admin_firestore().batch()
        stage_event(batch, event)
        batch.commit()

    await asyncio.to_thread(_commit)
    return event.event_id


async def emit_if_subscribed(
    uid: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    source: str = "",
    presence: bool = False,
) -> str | None:
    """Emit only when a registered agent subscribes to this type, so a high-frequency
    behavioral signal (app_open, content_viewed) with no consumer never costs a no-op
    orchestrate. Returns the event_id, or None if nothing subscribes. ``presence=True``
    adds the best-effort inline dispatch for ~1s latency. Registering a future agent
    for the type flips this on with no producer change."""
    from .registry import get_agent_registry

    if not get_agent_registry().agents_for_event(event_type):
        return None
    event_id = await emit(uid, event_type, payload, source=source)
    if presence:
        await dispatch_inline(uid)
    return event_id


# ── Inline (presence) dispatch ───────────────────────────────────────────────
async def dispatch_inline(uid: str) -> None:
    """Best-effort post-commit enqueue for a presence event (§4.1). NOT a
    guarantee: a scaled-to-zero container wakes in ~2-10s and a lost enqueue is
    recovered by the per-minute sweep. Never raises into the request path."""
    try:
        await asyncio.to_thread(get_task_scheduler().schedule_orchestrate, uid)
    except Exception as exc:
        logger.warn("event_bus.dispatch_inline: enqueue failed (sweep will recover)", {
            "user_id": uid, "error": str(exc),
        })


# ── The relay (outbox sweep) ─────────────────────────────────────────────────
def _read_pending(limit: int) -> list[str]:
    """The distinct uids that have at least one unconsumed outbox event, oldest
    first, bounded. Needs the ``outbox`` COLLECTION_GROUP (consumed, ts) index."""
    snaps = (
        admin_firestore()
        .collection_group(OUTBOX_SUBCOLLECTION)
        .where(filter=fs.FieldFilter(FIELD_CONSUMED, "==", False))
        .order_by(FIELD_TS)
        .limit(limit)
        .stream()
    )
    uids: list[str] = []
    seen: set[str] = set()
    for snap in snaps:
        data = snap.to_dict() or {}
        uid = str(data.get(FIELD_UID, "")) or _uid_from_ref(snap.reference)
        if uid and uid not in seen:
            seen.add(uid)
            uids.append(uid)
    return uids


def _uid_from_ref(ref: Any) -> str:
    # users/{uid}/outbox/{event_id}: the grandparent doc id is the uid.
    try:
        return ref.parent.parent.id
    except Exception:
        return ""


async def dispatch_pending(*, limit: int = DISPATCH_BATCH_LIMIT) -> int:
    """Enqueue one coalesced orchestrate task per user with unconsumed events.
    Marks nothing — the orchestrator marks events consumed once it has drained
    them, so a lost task simply re-enqueues next sweep. Returns the number of users
    dispatched. Safe to run every minute — one indexed equality read when empty."""
    try:
        uids = await asyncio.to_thread(_read_pending, limit)
    except Exception as exc:
        # A missing index 400s here; log loudly (a swept-nothing must never look
        # like a healthy empty sweep, per CLAUDE.md).
        logger.error("event_bus.dispatch_pending: read failed (missing index?)", {
            "error": str(exc),
        })
        return 0

    if not uids:
        return 0

    scheduler = get_task_scheduler()
    dispatched = 0
    for uid in uids:
        try:
            await asyncio.to_thread(scheduler.schedule_orchestrate, uid)
            dispatched += 1
        except Exception as exc:
            # Events stay consumed==false; the next sweep retries the enqueue.
            logger.error("event_bus.dispatch_pending: enqueue failed (will retry next sweep)", {
                "user_id": uid, "error": str(exc),
            })

    logger.info("event_bus.dispatch_pending: swept", {"users_dispatched": dispatched})
    return dispatched
