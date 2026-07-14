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
from .fact_gate import FactState, extract_transition
from .models import Checkpoint, Fixture, TrackedTopic, Tracker

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


def _fixtures_col(topic_key: str) -> fs.CollectionReference:
    return _topics_col().document(topic_key).collection(f.COLLECTION_FIXTURES)


def _fixture_fires_col(topic_key: str, fixture_id: str) -> fs.CollectionReference:
    return _fixtures_col(topic_key).document(fixture_id).collection(f.COLLECTION_FIXTURE_FIRES)


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


# The per-topic fire lease that used to live here was replaced by
# ``commit_fact_transition``'s compare-and-set (below): two moments racing on one
# real-world outcome now serialize on the FACT, not on a time-boxed lock.


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


async def latest_tracker_update_at(user_id: str) -> datetime | None:
    """The most recent time ANY of this user's trackers delivered an update, or None.

    Lets a proactive decider tell whether the user already got a tracker push recently
    (e.g. today) so it can avoid stacking another notification on the same day. Reuses
    the auto-indexed per-user tracker query; returns None on no trackers / read error."""
    trackers = await list_trackers_for_user(user_id)
    stamps = [t.last_update_at for t in trackers if t.last_update_at is not None]
    return max(stamps) if stamps else None


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
    summary: str,
    at: datetime,
) -> None:
    """Per-user delivery bookkeeping after a tracker update is sent. Increments the sent
    counter and advances the per-user dedup cursor (last_sent_summary) so two users on
    one topic don't share a send cursor."""
    updates: dict[str, Any] = {
        f.TRACKER_UPDATES_SENT: fs.Increment(1),
        f.TRACKER_LAST_UPDATE_AT: at,
        f.TRACKER_LAST_SENT_SUMMARY: summary,
        f.TRACKER_UPDATED_AT: datetime.now(UTC),
    }

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


async def upsert_moment_schedule(checkpoints: list[Checkpoint]) -> None:
    """Upsert MOMENT checkpoints without ever resurrecting a settled one. A missing
    doc is created whole (pending); an existing PENDING doc gets its schedule fields
    (fire_at, label, wake_override) refreshed in place; a doc in any other status —
    fired, skipped, claimed mid-flight — is left completely alone. This is what
    ``upsert_checkpoints``'s merge-write cannot guarantee (its full-doc merge includes
    ``status: pending``, which would re-arm an already-fired moment on reconcile)."""
    if not checkpoints:
        return

    def _write() -> None:
        db = admin_firestore()
        for cp in checkpoints:
            ref = _checkpoints_col().document(cp.id)
            transaction = db.transaction()

            @fs.transactional
            def _apply(txn: fs.Transaction, ref=ref, cp=cp) -> None:
                snap = ref.get(transaction=txn)
                if not snap.exists:
                    txn.set(ref, cp.to_dict())
                    return
                if (snap.to_dict() or {}).get(f.CHECKPOINT_STATUS) != f.CHECKPOINT_STATUS_PENDING:
                    return
                txn.update(ref, {
                    f.CHECKPOINT_FIRE_AT: cp.fire_at,
                    f.CHECKPOINT_EVENT_LABEL: cp.event_label,
                    f.CHECKPOINT_WAKE_OVERRIDE: cp.wake_override,
                    f.CHECKPOINT_FIXTURE_ID: cp.fixture_id,
                })

            _apply(transaction)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("tracking_store: upsert_moment_schedule failed", {
            "count": len(checkpoints), "error": str(exc),
        })


async def mark_checkpoint(checkpoint_id: str, status: str, **extra: Any) -> None:
    """Move a checkpoint to a terminal/intermediate state with optional extra fields
    (last_summary, last_fetch_tier, last_error, fired_at)."""
    def _update() -> None:
        _checkpoints_col().document(checkpoint_id).update({f.CHECKPOINT_STATUS: status, **extra})

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("tracking_store: mark_checkpoint failed", {"checkpoint_id": checkpoint_id, "error": str(exc)})


async def expire_checkpoint_if_pending(checkpoint_id: str) -> None:
    """Expire a checkpoint ONLY while it is still pending — a cancelled fixture's
    moments are retired without ever rewriting one that already fired/claimed.
    A missing doc is a no-op."""

    def _txn() -> None:
        db = admin_firestore()
        ref = _checkpoints_col().document(checkpoint_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return
            if (snap.to_dict() or {}).get(f.CHECKPOINT_STATUS) != f.CHECKPOINT_STATUS_PENDING:
                return
            txn.update(ref, {f.CHECKPOINT_STATUS: f.CHECKPOINT_STATUS_EXPIRED})

        _apply(transaction)

    try:
        await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("tracking_store: expire_checkpoint_if_pending failed", {
            "checkpoint_id": checkpoint_id, "error": str(exc),
        })


async def create_checkpoint_if_absent(checkpoint: Checkpoint) -> bool:
    """Create a checkpoint only when its id does not already exist. Returns True iff it
    was created. Used to SEED the recurring pulse exactly once per topic (provision +
    reconcile self-heal both call it) without ever resetting a live pulse's fire_at."""

    def _write() -> bool:
        ref = _checkpoints_col().document(checkpoint.id)
        if ref.get().exists:
            return False
        ref.set(checkpoint.to_dict())
        return True

    try:
        return await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("tracking_store: create_checkpoint_if_absent failed", {
            "checkpoint_id": checkpoint.id, "error": str(exc),
        })
        return False


async def rearm_pulse(
    checkpoint_id: str, *, fire_at: datetime, tier: str, at: datetime, summary: str | None = None,
) -> None:
    """Re-arm the recurring pulse: reset it to PENDING with a fresh fire_at so it fires
    again next cycle (the pulse is the one checkpoint that is never terminal). Clears
    claimed_at and records the last fetch tier/time; advances last_summary only when the
    pulse actually delivered something new."""
    extra: dict[str, Any] = {
        f.CHECKPOINT_FIRE_AT: fire_at,
        f.CHECKPOINT_CLAIMED_AT: None,
        f.CHECKPOINT_LAST_FETCH_TIER: tier,
        f.CHECKPOINT_LAST_FETCH_AT: at,
    }
    if summary is not None:
        extra[f.CHECKPOINT_LAST_SUMMARY] = summary
        extra[f.CHECKPOINT_FIRED_AT] = at
    await mark_checkpoint(checkpoint_id, f.CHECKPOINT_STATUS_PENDING, **extra)


# ── fixtures (stable identity + fact state) ──────────────────────────────────
async def list_fixtures(topic_key: str) -> list[Fixture]:
    """All fixture docs of a topic (parent-scoped read, no index needed). A topic
    holds at most a few dozen fixtures, so no pagination."""

    def _fetch() -> list[Fixture]:
        return [Fixture.from_dict(s.to_dict() or {}) for s in _fixtures_col(topic_key).stream()]

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("tracking_store: list_fixtures failed", {"topic_key": topic_key, "error": str(exc)})
        return []


async def get_fixture(topic_key: str, fixture_id: str) -> Fixture | None:
    def _fetch() -> Fixture | None:
        snap = _fixtures_col(topic_key).document(fixture_id).get()
        if not snap.exists:
            return None
        return Fixture.from_dict(snap.to_dict() or {})

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("tracking_store: get_fixture failed", {
            "topic_key": topic_key, "fixture_id": fixture_id, "error": str(exc),
        })
        return None


async def upsert_fixtures(topic_key: str, fixtures: list[Fixture]) -> None:
    """Write reconcile results: a missing doc is created whole; an existing doc gets
    ONLY its schedule/identity fields refreshed (label, times, kind, wake_override).
    Fact state is deliberately untouched here — it belongs to
    ``commit_fact_transition``'s transaction, so a reconcile racing a result fire can
    never clobber a just-committed outcome with the stale facts it read earlier."""
    if not fixtures:
        return

    def _write() -> None:
        db = admin_firestore()
        for fx in fixtures:
            ref = _fixtures_col(topic_key).document(fx.id)
            transaction = db.transaction()

            @fs.transactional
            def _apply(txn: fs.Transaction, ref=ref, fx=fx) -> None:
                snap = ref.get(transaction=txn)
                if not snap.exists:
                    txn.set(ref, fx.to_dict())
                    return
                txn.update(ref, {
                    f.FIXTURE_LABEL: fx.label,
                    f.FIXTURE_START_AT: fx.start_at,
                    f.FIXTURE_EXPECTED_END_AT: fx.expected_end_at,
                    f.FIXTURE_KIND: fx.kind,
                    f.FIXTURE_LEAD_MINUTES: fx.lead_minutes,
                    f.FIXTURE_WAKE_OVERRIDE: fx.wake_override,
                    f.FIXTURE_UPDATED_AT: fx.updated_at or datetime.now(UTC),
                })

            _apply(transaction)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("tracking_store: upsert_fixtures failed", {
            "topic_key": topic_key, "count": len(fixtures), "error": str(exc),
        })


async def cancel_fixture(topic_key: str, fixture_id: str) -> None:
    """Mark a fixture dropped from the schedule. Only a still-SCHEDULED fixture is
    cancelled — one that went live/finished in the meantime keeps its real state."""

    def _txn() -> None:
        db = admin_firestore()
        ref = _fixtures_col(topic_key).document(fixture_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return
            if (snap.to_dict() or {}).get(f.FIXTURE_STATUS) != f.FIXTURE_STATUS_SCHEDULED:
                return
            txn.update(ref, {
                f.FIXTURE_STATUS: f.FIXTURE_STATUS_CANCELLED,
                f.FIXTURE_LAST_TRANSITION: f"{f.FIXTURE_STATUS_SCHEDULED}->{f.FIXTURE_STATUS_CANCELLED}",
                f.FIXTURE_UPDATED_AT: datetime.now(UTC),
            })

        _apply(transaction)

    try:
        await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("tracking_store: cancel_fixture failed", {
            "topic_key": topic_key, "fixture_id": fixture_id, "error": str(exc),
        })


async def commit_fact_transition(
    topic_key: str, fixture_id: str, seen: FactState, *, now: datetime,
) -> str | None:
    """Transactionally apply freshly-extracted facts to a fixture. The transition is
    recomputed INSIDE the transaction against the current stored facts, so of two
    moments racing on the same real-world outcome exactly one gets the transition
    string back (and sends); the loser gets None (already applied) and abstains.
    This compare-and-set is what replaced the per-topic fire lease."""

    def _txn() -> str | None:
        db = admin_firestore()
        ref = _fixtures_col(topic_key).document(fixture_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> str | None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return None
            current = Fixture.from_dict(snap.to_dict() or {})
            prior = FactState(
                status=current.status,
                score=current.fact_score,
                winner=current.fact_winner,
                note=current.fact_note,
            )
            transition = extract_transition(prior, seen)
            if transition is None:
                return None
            txn.update(ref, {
                f.FIXTURE_STATUS: seen.status,
                f.FIXTURE_FACT_SCORE: seen.score or current.fact_score,
                f.FIXTURE_FACT_WINNER: seen.winner or current.fact_winner,
                f.FIXTURE_FACT_NOTE: seen.note,
                f.FIXTURE_FACTS_UPDATED_AT: now,
                f.FIXTURE_LAST_TRANSITION: transition,
                f.FIXTURE_UPDATED_AT: now,
            })
            return transition

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("tracking_store: commit_fact_transition failed", {
            "topic_key": topic_key, "fixture_id": fixture_id, "error": str(exc),
        })
        return None


async def record_fire_audit(
    topic_key: str,
    fixture_id: str,
    *,
    moment: str,
    fired_at: datetime,
    decision: str,
    query: str = "",
    fetch_tier: str = "",
    prior_facts: dict[str, str] | None = None,
    seen_facts: dict[str, str] | None = None,
    transition: str = "",
    sent_count: int = 0,
    title: str = "",
) -> None:
    """One audit row per moment fire, SENT OR ABSTAINED. The per-user notification
    ledger already records deliveries; this records the abstains and re-arms, which
    is where "why didn't I get a notification" debugging lives. Fire-and-forget: an
    audit write failure must never affect the fire itself."""
    doc = {
        f.AUDIT_MOMENT: moment,
        f.AUDIT_FIRED_AT: fired_at,
        f.AUDIT_QUERY: query,
        f.AUDIT_FETCH_TIER: fetch_tier,
        f.AUDIT_PRIOR_FACTS: prior_facts or {},
        f.AUDIT_SEEN_FACTS: seen_facts or {},
        f.AUDIT_TRANSITION: transition,
        f.AUDIT_DECISION: decision,
        f.AUDIT_SENT_COUNT: sent_count,
        f.AUDIT_TITLE: title,
    }

    def _write() -> None:
        _fixture_fires_col(topic_key, fixture_id).add(doc)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("tracking_store: record_fire_audit failed", {
            "topic_key": topic_key, "fixture_id": fixture_id, "decision": decision, "error": str(exc),
        })


async def try_claim_tracker_daily_slot(
    tracker_id: str, *, today: str, cap: int, force: bool = False,
) -> bool:
    """Atomically claim one of the tracker's daily send slots (the per-user-per-topic
    ceiling, founder decision: 8/day). A fire on a new UTC date resets the counter in
    the same transaction. ``force`` (a wake_override result — a final's outcome must
    land) bypasses the cap but still increments, so the day's count stays honest.
    Fails OPEN like notification_budget: a store error never blocks a send the user
    asked for."""

    def _txn() -> bool:
        db = admin_firestore()
        ref = _trackers_col().document(tracker_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> bool:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            sent_today = int(data.get(f.TRACKER_SENT_TODAY, 0) or 0)
            if str(data.get(f.TRACKER_SENT_TODAY_DATE, "") or "") != today:
                sent_today = 0
            if not force and sent_today >= cap:
                return False
            txn.update(ref, {
                f.TRACKER_SENT_TODAY: sent_today + 1,
                f.TRACKER_SENT_TODAY_DATE: today,
            })
            return True

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("tracking_store: try_claim_tracker_daily_slot failed (fail-open)", {
            "tracker_id": tracker_id, "error": str(exc),
        })
        return True


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
