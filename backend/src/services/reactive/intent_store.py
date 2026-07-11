"""Pending intents — revocable scheduled actions.

The first-class principle of the reactive layer: a scheduled action is an INTENT the
world can invalidate, never fire-and-forget. Buddy says "I'll check how your mom's
surgery goes" -> a pending intent with ``fire_at`` after the surgery. If the user
later says "mom's home and fine," a resolution event cancels the intent BEFORE it
fires. If the intent already fired, a resolution tombstone (``resolved_topics``)
stops it from being re-created (the late-event resurrection guard).

``subject`` is a closed-set resolution key (a stable slug), never free text — so
"mom's operation" and "mom is fine" resolve the same intent even though the words
differ. One open intent per subject at a time: the doc id IS the subject hash, so a
re-detection overwrites rather than duplicating.

Atomicity rules (CLAUDE.md / the design's revalidate-on-fire):
  * claim a due intent with a transaction (pending -> fired) so two sweeps never
    double-fire it;
  * cancel with a transaction that ALSO writes the tombstone, so a resolution that
    races the fire either cancels first (no send) or finds it already fired (tombstone
    blocks re-creation) — never both.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from google.cloud import firestore as fs  # type: ignore

from ...lib.logger import logger
from ..firebase import admin_firestore
from .fields import (
    FIELD_CANCELLED_AT,
    FIELD_CREATED_AT,
    FIELD_DEDUP_ID,
    FIELD_EXPIRES_AT,
    FIELD_FIRE_AT,
    FIELD_FIRED_AT,
    FIELD_INTENT_ID,
    FIELD_KIND,
    FIELD_QUESTION,
    FIELD_RESOLUTION_REASON,
    FIELD_SOURCE,
    FIELD_STATUS,
    FIELD_SUBJECT,
    FIELD_TS,
    FIELD_UID,
    INTENT_CANCELLED,
    INTENT_FIRED,
    INTENT_PENDING,
    INTENT_TTL,
    INTENTS_SUBCOLLECTION,
    RESOLVED_TOPIC_TTL,
    RESOLVED_TOPICS_SUBCOLLECTION,
    USERS_COLLECTION,
)

# Bound the sweep so a backlog never bursts the per-minute tick.
CLAIM_BATCH_LIMIT = 100


@dataclass
class Intent:
    intent_id: str
    uid: str
    kind: str
    subject: str
    question: str
    fire_at: datetime
    status: str
    source: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Intent:
        return cls(
            intent_id=str(data.get(FIELD_INTENT_ID, "")),
            uid=str(data.get(FIELD_UID, "")),
            kind=str(data.get(FIELD_KIND, "")),
            subject=str(data.get(FIELD_SUBJECT, "")),
            question=str(data.get(FIELD_QUESTION, "")),
            fire_at=_as_aware(data.get(FIELD_FIRE_AT)) or datetime.now(UTC),
            status=str(data.get(FIELD_STATUS, "")),
            source=str(data.get(FIELD_SOURCE, "")),
        )


def subject_id(subject: str) -> str:
    """Stable doc id for a subject so one concern owns at most one open intent."""
    return hashlib.sha1(subject.strip().casefold().encode("utf-8")).hexdigest()[:16]


def _as_aware(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _intents_col(uid: str):
    return (
        admin_firestore()
        .collection(USERS_COLLECTION)
        .document(uid)
        .collection(INTENTS_SUBCOLLECTION)
    )


def _resolved_ref(uid: str, subject: str):
    return (
        admin_firestore()
        .collection(USERS_COLLECTION)
        .document(uid)
        .collection(RESOLVED_TOPICS_SUBCOLLECTION)
        .document(subject_id(subject))
    )


# ── Scheduling ───────────────────────────────────────────────────────────────
async def schedule_intent(
    uid: str,
    *,
    kind: str,
    subject: str,
    question: str,
    fire_at: datetime,
    source: str = "",
    now: datetime | None = None,
) -> str | None:
    """Create a pending intent for ``subject``, or return ``None`` if it should not
    be scheduled: the subject was resolved recently (tombstone), or an open intent
    for it already exists. Idempotent on the subject (doc id is the subject hash)."""
    when = now or datetime.now(UTC)
    subject = subject.strip()
    if not subject:
        return None

    def _create() -> str | None:
        # All three reads + the write run inside ONE transaction so a concurrent
        # cancel_pending_by_subject that lands between the tombstone check and the
        # intent write cannot create an intent that outlives its own cancellation.
        db = admin_firestore()
        intent_ref = _intents_col(uid).document(subject_id(subject))
        tombstone_ref = _resolved_ref(uid, subject)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> str | None:
            resolved = tombstone_ref.get(transaction=txn)
            if resolved.exists:
                exp = _as_aware((resolved.to_dict() or {}).get(FIELD_EXPIRES_AT))
                if exp is None or exp > when:
                    logger.info("intent_store: skip schedule, subject recently resolved", {
                        "user_id": uid, "subject": subject,
                    })
                    return None

            existing = intent_ref.get(transaction=txn)
            if existing.exists and (existing.to_dict() or {}).get(FIELD_STATUS) == INTENT_PENDING:
                logger.info("intent_store: skip schedule, open intent already exists", {
                    "user_id": uid, "subject": subject,
                })
                return None

            intent_id = subject_id(subject)
            txn.set(intent_ref, {
                FIELD_INTENT_ID: intent_id,
                FIELD_UID: uid,
                FIELD_KIND: kind,
                FIELD_SUBJECT: subject,
                FIELD_QUESTION: question,
                FIELD_FIRE_AT: fire_at,
                FIELD_STATUS: INTENT_PENDING,
                FIELD_SOURCE: source,
                FIELD_CREATED_AT: when,
                FIELD_DEDUP_ID: intent_id,
                # A never-fired intent still gets reaped: its TTL is well past its fire time.
                FIELD_EXPIRES_AT: fire_at + INTENT_TTL,
            })
            return intent_id

        return _apply(transaction)

    try:
        intent_id = await asyncio.to_thread(_create)
    except Exception as exc:
        logger.warn("intent_store.schedule_intent failed", {
            "user_id": uid, "subject": subject, "error": str(exc),
        })
        return None

    if intent_id:
        logger.info("intent_store: intent scheduled", {
            "user_id": uid, "subject": subject, "kind": kind, "fire_at": fire_at.isoformat(),
        })
    return intent_id


# ── Firing (the supervisor's atomic claim) ───────────────────────────────────
def _claim_due(now: datetime, limit: int) -> list[Intent]:
    # Lazy import: event_bus -> intent_store would be circular at module load.
    from .event_bus import build_event, stage_event
    from .events import EVENT_INTENT_DUE

    db = admin_firestore()
    snaps = (
        db.collection_group(INTENTS_SUBCOLLECTION)
        .where(filter=fs.FieldFilter(FIELD_STATUS, "==", INTENT_PENDING))
        .where(filter=fs.FieldFilter(FIELD_FIRE_AT, "<=", now))
        .order_by(FIELD_FIRE_AT)
        .limit(limit)
        .stream()
    )
    claimed: list[Intent] = []
    for snap in snaps:
        ref = snap.reference
        preview = Intent.from_dict(snap.to_dict() or {})
        if not preview.uid:
            logger.warn("intent_store: intent doc missing uid, skipping", {"path": ref.path})
            continue

        # Build the outbox event BEFORE the transaction (pure, no I/O) so
        # stage_event can commit atomically with the status flip inside _apply.
        # This closes the gap where a killed instance leaves the intent in FIRED
        # with no matching outbox event, stranding it forever.
        outbox_event = build_event(
            preview.uid,
            EVENT_INTENT_DUE,
            payload={
                "intent_id": preview.intent_id,
                "subject": preview.subject,
                "question": preview.question,
                "kind": preview.kind,
            },
            source="intent_supervisor",
        )

        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction, ref=ref, event=outbox_event) -> dict | None:
            fresh = ref.get(transaction=txn)
            data = fresh.to_dict() or {}
            if data.get(FIELD_STATUS) != INTENT_PENDING:
                return None  # another sweep claimed it
            txn.update(ref, {FIELD_STATUS: INTENT_FIRED, FIELD_FIRED_AT: now})
            stage_event(txn, event)  # atomic: intent fired ↔ outbox event exist together
            return data

        try:
            data = _apply(transaction)
        except Exception as exc:
            logger.warn("intent_store: claim transaction failed", {"error": str(exc)})
            continue
        if data is not None:
            claimed.append(Intent.from_dict(data))
    return claimed


async def claim_due_intents(
    *, now: datetime | None = None, limit: int = CLAIM_BATCH_LIMIT
) -> list[Intent]:
    """Atomically claim all pending intents whose ``fire_at`` has passed (pending ->
    fired). Returns the claimed intents so the supervisor can emit one ``intent_due``
    event each. Needs the ``intents`` COLLECTION_GROUP (status, fire_at) index."""
    when = now or datetime.now(UTC)
    try:
        return await asyncio.to_thread(_claim_due, when, limit)
    except Exception as exc:
        logger.error("intent_store.claim_due_intents failed (missing index?)", {"error": str(exc)})
        return []


# ── Cancellation (the invalidation) ──────────────────────────────────────────
async def cancel_pending_by_subject(
    uid: str, subject: str, *, reason: str, now: datetime | None = None
) -> bool:
    """Cancel the open intent for ``subject`` and write a resolution tombstone, in one
    transaction. Returns True iff a pending intent was cancelled. The tombstone is
    written even if no pending intent existed (it may have already fired — the guard
    must still block a re-create)."""
    when = now or datetime.now(UTC)
    subject = subject.strip()
    if not subject:
        return False

    def _cancel() -> bool:
        db = admin_firestore()
        intent_ref = _intents_col(uid).document(subject_id(subject))
        tombstone_ref = _resolved_ref(uid, subject)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> bool:
            snap = intent_ref.get(transaction=txn)
            cancelled = False
            if snap.exists and (snap.to_dict() or {}).get(FIELD_STATUS) == INTENT_PENDING:
                txn.update(intent_ref, {
                    FIELD_STATUS: INTENT_CANCELLED,
                    FIELD_CANCELLED_AT: when,
                    FIELD_RESOLUTION_REASON: reason[:300],
                })
                cancelled = True
            txn.set(tombstone_ref, {
                FIELD_UID: uid,
                FIELD_SUBJECT: subject,
                FIELD_RESOLUTION_REASON: reason[:300],
                FIELD_TS: when,
                FIELD_EXPIRES_AT: when + RESOLVED_TOPIC_TTL,
            })
            return cancelled

        return _apply(transaction)

    try:
        cancelled = await asyncio.to_thread(_cancel)
    except Exception as exc:
        logger.warn("intent_store.cancel_pending_by_subject failed", {
            "user_id": uid, "subject": subject, "error": str(exc),
        })
        return False

    logger.info("intent_store: subject resolved", {
        "user_id": uid, "subject": subject, "cancelled_pending": cancelled, "reason": reason[:80],
    })
    return cancelled


# ── Reads (for the closed-set classifier) ────────────────────────────────────
async def list_open_subjects(uid: str, *, limit: int = 50) -> list[Intent]:
    """All pending intents for a user (the closed set a resolution classifier picks
    from). Single equality at collection scope -> auto-indexed, no declaration."""

    def _fetch() -> list[Intent]:
        snaps = (
            _intents_col(uid)
            .where(filter=fs.FieldFilter(FIELD_STATUS, "==", INTENT_PENDING))
            .limit(limit)
            .stream()
        )
        return [Intent.from_dict(snap.to_dict() or {}) for snap in snaps]

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("intent_store.list_open_subjects failed", {"user_id": uid, "error": str(exc)})
        return []
