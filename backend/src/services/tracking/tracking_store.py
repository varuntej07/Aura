"""Firestore access layer for topic tracking.

All Firebase Admin SDK calls are blocking, so every public function is an ``async``
wrapper that dispatches the sync work via ``asyncio.to_thread`` — matching
``threads.thread_store`` and ``signal_engine.feature_store``. Read failures are
logged and degrade to an empty/default result so a tracker tick can never crash a
scheduler tick.

Three TOP-LEVEL flat collections (not per-user subcollections), so the hot due-scan
is a tight indexed range query rather than a collection_group fan-out:

  tracked_topics/{topic_key}   — shared topic + schedule health
  trackers/{tracker_id}        — per-user subscription
  checkpoints/{checkpoint_id}  — the flat due-queue (status, fire_at) composite index

Two queries need the composite indexes declared in firestore.indexes.json:
  fetch_due_checkpoints        — checkpoints (status ASC, fire_at ASC)
  list_topics_due_for_reconcile — tracked_topics (status ASC, next_reconcile_at ASC)
A missing index makes these 400 at runtime (not import time); if swallowed it looks
identical to "no data" (CLAUDE.md Firestore-index lesson), so both are declared.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore as fs
from google.cloud.firestore_v1.base_query import FieldFilter

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as f
from .models import Checkpoint, TrackedTopic, Tracker

# Caps so a runaway topic/profile can never pull an unbounded page into memory.
MAX_DUE_CHECKPOINTS = 200
MAX_DUE_TOPICS = 100
MAX_SUBSCRIBERS_READ = 500
MAX_TRACKERS_PER_USER = 100
MAX_CHECKPOINTS_PER_TOPIC = 300


def _topics_col() -> fs.CollectionReference:
    return admin_firestore().collection(f.COLLECTION_TRACKED_TOPICS)


def _trackers_col() -> fs.CollectionReference:
    return admin_firestore().collection(f.COLLECTION_TRACKERS)


def _checkpoints_col() -> fs.CollectionReference:
    return admin_firestore().collection(f.COLLECTION_CHECKPOINTS)


# ── tracked_topics ───────────────────────────────────────────────────────────
async def get_tracked_topic(topic_key: str) -> TrackedTopic | None:
    def _fetch() -> TrackedTopic | None:
        snap = _topics_col().document(topic_key).get()
        if not snap.exists:
            return None
        return TrackedTopic.from_dict(snap.to_dict() or {})

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("tracking_store: get_tracked_topic failed", {"topic_key": topic_key, "error": str(exc)})
        return None


async def set_tracked_topic(topic: TrackedTopic) -> None:
    """Idempotent upsert keyed by topic_key (re-research overwrites the doc)."""

    def _write() -> None:
        _topics_col().document(topic.topic_key).set(topic.to_dict())

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("tracking_store: set_tracked_topic failed", {"topic_key": topic.topic_key, "error": str(exc)})


async def update_tracked_topic(topic_key: str, updates: dict[str, Any]) -> None:
    """Partial update. Callers pass keys built from fields.py constants. Always
    stamps the updated_at so a glance shows the last write."""

    def _update() -> None:
        _topics_col().document(topic_key).update({**updates, f.TOPIC_UPDATED_AT: datetime.now(UTC)})

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("tracking_store: update_tracked_topic failed", {"topic_key": topic_key, "error": str(exc)})


async def adjust_subscriber_count(topic_key: str, delta: int) -> None:
    """Atomically move the shared subscriber counter (a tracker created/cancelled)."""

    def _update() -> None:
        _topics_col().document(topic_key).update({
            f.TOPIC_SUBSCRIBER_COUNT: fs.Increment(delta),
            f.TOPIC_UPDATED_AT: datetime.now(UTC),
        })

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("tracking_store: adjust_subscriber_count failed", {"topic_key": topic_key, "error": str(exc)})


async def update_topic_live_cache(topic_key: str, *, summary: str, fetched_at: datetime, tier: str) -> None:
    """Write the shared live-state cache after one fetch, fanned to all subscribers."""
    await update_tracked_topic(topic_key, {
        f.TOPIC_LIVE_SUMMARY: summary,
        f.TOPIC_LIVE_FETCHED_AT: fetched_at,
        f.TOPIC_LIVE_SOURCE_TIER: tier,
    })


async def set_topic_status(topic_key: str, status: str) -> None:
    await update_tracked_topic(topic_key, {f.TOPIC_STATUS: status})


async def list_topics_due_for_reconcile(now: datetime, limit: int = MAX_DUE_TOPICS) -> list[TrackedTopic]:
    """Active topics whose next_reconcile_at has passed. Needs the
    tracked_topics (status ASC, next_reconcile_at ASC) composite index."""

    def _fetch() -> list[TrackedTopic]:
        snaps = (
            _topics_col()
            .where(filter=FieldFilter(f.TOPIC_STATUS, "==", f.TOPIC_STATUS_ACTIVE))
            .where(filter=FieldFilter(f.TOPIC_NEXT_RECONCILE_AT, "<=", now))
            .order_by(f.TOPIC_NEXT_RECONCILE_AT)
            .limit(limit)
            .stream()
        )
        return [TrackedTopic.from_dict(s.to_dict() or {}) for s in snaps]

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("tracking_store: list_topics_due_for_reconcile failed", {"error": str(exc)})
        return []


# ── trackers (per-user) ──────────────────────────────────────────────────────
async def create_tracker(tracker: Tracker) -> None:
    def _write() -> None:
        _trackers_col().document(tracker.id).set(tracker.to_dict())

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("tracking_store: create_tracker failed", {"tracker_id": tracker.id, "error": str(exc)})


async def get_tracker(tracker_id: str) -> Tracker | None:
    def _fetch() -> Tracker | None:
        snap = _trackers_col().document(tracker_id).get()
        if not snap.exists:
            return None
        return Tracker.from_dict(snap.to_dict() or {})

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("tracking_store: get_tracker failed", {"tracker_id": tracker_id, "error": str(exc)})
        return None


async def list_trackers_for_user(user_id: str) -> list[Tracker]:
    """All of a user's trackers. Single-equality filter (user_id ==) at collection
    scope — auto-indexed, no composite needed; status filtering is left to the caller."""

    def _fetch() -> list[Tracker]:
        snaps = (
            _trackers_col()
            .where(filter=FieldFilter(f.TRACKER_USER_ID, "==", user_id))
            .limit(MAX_TRACKERS_PER_USER)
            .stream()
        )
        return [Tracker.from_dict(s.to_dict() or {}) for s in snaps]

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("tracking_store: list_trackers_for_user failed", {"user_id": user_id, "error": str(exc)})
        return []


async def list_active_subscribers(topic_key: str) -> list[Tracker]:
    """Active subscribers of a shared topic, for the per-checkpoint fan-out. Single
    equality (topic_key ==) at collection scope (auto-indexed); the active filter is
    applied in Python so no composite index is needed."""

    def _fetch() -> list[Tracker]:
        snaps = (
            _trackers_col()
            .where(filter=FieldFilter(f.TRACKER_TOPIC_KEY, "==", topic_key))
            .limit(MAX_SUBSCRIBERS_READ)
            .stream()
        )
        out: list[Tracker] = []
        for s in snaps:
            t = Tracker.from_dict(s.to_dict() or {})
            if t.status == f.TRACKER_STATUS_ACTIVE:
                out.append(t)
        return out

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("tracking_store: list_active_subscribers failed", {"topic_key": topic_key, "error": str(exc)})
        return []


async def set_tracker_status(tracker_id: str, status: str) -> None:
    def _update() -> None:
        _trackers_col().document(tracker_id).update({
            f.TRACKER_STATUS: status,
            f.TRACKER_UPDATED_AT: datetime.now(UTC),
        })

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("tracking_store: set_tracker_status failed", {"tracker_id": tracker_id, "error": str(exc)})


async def record_tracker_outcome(
    tracker_id: str,
    *,
    decision: str,
    reason: str,
    summary: str,
    at: datetime,
) -> None:
    """Per-user delivery bookkeeping after the gatekeeper rules on a candidate.
    Increments the matching counter (sent/held/dropped) and, on a send, advances the
    per-user dedup cursor (last_sent_summary) so two users on one topic don't share it."""
    updates: dict[str, Any] = {
        f.TRACKER_LAST_GATEKEEPER_REASON: reason,
        f.TRACKER_UPDATED_AT: datetime.now(UTC),
    }
    if decision == f.DECISION_SEND:
        updates[f.TRACKER_UPDATES_SENT] = fs.Increment(1)
        updates[f.TRACKER_LAST_UPDATE_AT] = at
        updates[f.TRACKER_LAST_SENT_SUMMARY] = summary
    elif decision == f.DECISION_HOLD:
        updates[f.TRACKER_UPDATES_HELD] = fs.Increment(1)
    elif decision == f.DECISION_DROP:
        updates[f.TRACKER_UPDATES_DROPPED] = fs.Increment(1)

    def _update() -> None:
        _trackers_col().document(tracker_id).update(updates)

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("tracking_store: record_tracker_outcome failed", {"tracker_id": tracker_id, "error": str(exc)})


async def delete_trackers_for_user(user_id: str) -> int:
    """Delete every tracker owned by a user (account-deletion completeness for the
    flat collection — the per-user docs are not under users/{uid}). Decrements each
    topic's subscriber_count so an abandoned topic can later go stale + be cleaned.
    Returns the number deleted. Shared tracked_topics/checkpoints are never owned by
    one user, so they are not deleted here."""

    def _delete() -> int:
        snaps = list(
            _trackers_col().where(filter=FieldFilter(f.TRACKER_USER_ID, "==", user_id)).stream()
        )
        count = 0
        for s in snaps:
            data = s.to_dict() or {}
            topic_key = str(data.get(f.TRACKER_TOPIC_KEY, ""))
            try:
                s.reference.delete()
                if topic_key:
                    _topics_col().document(topic_key).update({
                        f.TOPIC_SUBSCRIBER_COUNT: fs.Increment(-1),
                    })
                count += 1
            except Exception as exc:
                logger.warn("tracking_store: delete one tracker failed", {
                    "user_id": user_id, "tracker_id": s.id, "error": str(exc),
                })
        return count

    try:
        return await asyncio.to_thread(_delete)
    except Exception as exc:
        logger.warn("tracking_store: delete_trackers_for_user failed", {"user_id": user_id, "error": str(exc)})
        return 0


# ── checkpoints (the flat due-queue) ─────────────────────────────────────────
async def fetch_due_checkpoints(now: datetime, limit: int = MAX_DUE_CHECKPOINTS) -> list[Checkpoint]:
    """Pending checkpoints whose fire_at has passed, oldest first. Needs the
    checkpoints (status ASC, fire_at ASC) composite index. Mirrors
    tool_executor.fetch_due_reminders, but on a flat collection (no parent walk)."""

    def _fetch() -> list[Checkpoint]:
        snaps = (
            _checkpoints_col()
            .where(filter=FieldFilter(f.CHECKPOINT_STATUS, "==", f.CHECKPOINT_STATUS_PENDING))
            .where(filter=FieldFilter(f.CHECKPOINT_FIRE_AT, "<=", now))
            .order_by(f.CHECKPOINT_FIRE_AT)
            .limit(limit)
            .stream()
        )
        return [Checkpoint.from_dict(s.to_dict() or {}) for s in snaps]

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("tracking_store: fetch_due_checkpoints failed", {"error": str(exc)})
        return []


async def claim_checkpoint(checkpoint_id: str) -> bool:
    """Atomically claim a pending checkpoint (pending -> claimed) so two overlapping
    ticks can never both fire it. Returns True iff this caller claimed it. Mirrors
    tool_executor.claim_reminder_for_processing."""

    def _claim() -> bool:
        db = admin_firestore()
        ref = db.collection(f.COLLECTION_CHECKPOINTS).document(checkpoint_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> bool:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return False
            if (snap.to_dict() or {}).get(f.CHECKPOINT_STATUS) != f.CHECKPOINT_STATUS_PENDING:
                return False
            txn.update(ref, {
                f.CHECKPOINT_STATUS: f.CHECKPOINT_STATUS_CLAIMED,
                f.CHECKPOINT_CLAIMED_AT: datetime.now(UTC),
                f.CHECKPOINT_ATTEMPTS: fs.Increment(1),
            })
            return True

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_claim)
    except Exception as exc:
        logger.warn("tracking_store: claim_checkpoint failed", {"checkpoint_id": checkpoint_id, "error": str(exc)})
        return False


async def upsert_checkpoints(checkpoints: list[Checkpoint]) -> None:
    """Idempotent batch upsert keyed by checkpoint id (reconcile re-materialization).
    merge=True so a re-upsert that shifts fire_at never clobbers an in-flight
    status/last_summary that a fire already wrote."""
    if not checkpoints:
        return

    def _write() -> None:
        db = admin_firestore()
        batch = db.batch()
        for cp in checkpoints:
            ref = db.collection(f.COLLECTION_CHECKPOINTS).document(cp.id)
            batch.set(ref, cp.to_dict(), merge=True)
        batch.commit()

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("tracking_store: upsert_checkpoints failed", {"count": len(checkpoints), "error": str(exc)})


async def mark_checkpoint(checkpoint_id: str, status: str, **extra: Any) -> None:
    """Move a checkpoint to a terminal/intermediate state with optional extra fields
    (last_summary, last_fetch_tier, last_error, fired_at)."""
    def _update() -> None:
        _checkpoints_col().document(checkpoint_id).update({f.CHECKPOINT_STATUS: status, **extra})

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("tracking_store: mark_checkpoint failed", {"checkpoint_id": checkpoint_id, "error": str(exc)})


async def list_checkpoints_for_topic(topic_key: str) -> list[Checkpoint]:
    """All checkpoints for a topic (for reconcile diffing). Single equality
    (topic_key ==) at collection scope — auto-indexed."""

    def _fetch() -> list[Checkpoint]:
        snaps = (
            _checkpoints_col()
            .where(filter=FieldFilter(f.CHECKPOINT_TOPIC_KEY, "==", topic_key))
            .limit(MAX_CHECKPOINTS_PER_TOPIC)
            .stream()
        )
        return [Checkpoint.from_dict(s.to_dict() or {}) for s in snaps]

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("tracking_store: list_checkpoints_for_topic failed", {"topic_key": topic_key, "error": str(exc)})
        return []
