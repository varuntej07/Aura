"""Durable per-generation record for ingest-triggered signal scoring.

Scoring used to run on its own recurring Cloud Scheduler job (every 15-30 min)
even though the content pool it scores against only changes on the 4-hour
content-ingest cadence — 16 near-identical KNN passes per ingest interval. Now
each completed ingest enqueues ONE durable Cloud Task that runs ONE scoring
pass, and this module is the correctness boundary that makes that pass
idempotent: Cloud Tasks delivers at-least-once, Cloud Scheduler can retry an
ingest, and a manual recovery call can race a live task — all of them converge
on the same per-generation document and only one of them actually scores.

Each 4-hour UTC ingest window is a GENERATION with a deterministic ID
(e.g. "20260709T1200Z" for the 12:00-16:00 UTC bucket, six per UTC day). The
generation record is a single direct-access document at
signal_engine_generations/{generation_id} — no queries, no collection scans,
no ledger, therefore no firestore.indexes.json entry (direct doc reads never
need an index).

Lifecycle of scoring_status:
  pending  -> written by record_ingest_completed when an ingest finishes.
  running  -> set atomically by claim_for_scoring; guarded by a lease so a
              second concurrent delivery no-ops while the holder works, but a
              crashed holder's generation is reclaimable after lease expiry.
  complete -> terminal; every later delivery for this generation no-ops.
  failed   -> retryable; the Cloud Task retry (or a manual call) re-claims it.

All Firestore access is sync (firebase-admin) and wrapped in asyncio.to_thread.
claim_for_scoring fails CLOSED (raises) on a store error: the caller returns a
non-2xx so Cloud Tasks retries with backoff, which is strictly safer than
either silently skipping a generation or double-running one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from google.cloud import firestore as fs  # type: ignore

from ...lib.logger import logger
from ..firebase import admin_firestore

GENERATIONS_COLLECTION = "signal_engine_generations"

# Must match the content-ingest Cloud Scheduler cadence ("0 */4 * * *"). Six
# four-hour buckets per UTC day; test_signal_generation_id.py locks both the
# divisibility and the six-per-day contract.
GENERATION_WINDOW_HOURS = 4
GENERATIONS_PER_UTC_DAY = 24 // GENERATION_WINDOW_HOURS

# How long one scoring claim is protected from being reclaimed. Well above the
# worst-case run_tick duration (per-user work is concurrent and each framer LLM
# call is capped at 10s), well below the 4h generation window so a crashed
# worker's generation is retried long before the next generation starts.
SCORING_LEASE_SECONDS = 15 * 60

# scoring_status values (see module docstring for the lifecycle).
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

# Field names — single source shared by the writer (this module) and any
# reader (ops tooling), per the data-layer field-constant discipline.
FIELD_GENERATION_ID = "generation_id"
FIELD_INGEST_COMPLETED_AT = "ingest_completed_at"
FIELD_NEW_CANDIDATES_WRITTEN = "new_candidates_written"
FIELD_SCORING_STATUS = "scoring_status"
FIELD_LEASE_EXPIRES_AT = "lease_expires_at"
FIELD_SCORING_STARTED_AT = "scoring_started_at"
FIELD_SCORING_COMPLETED_AT = "scoring_completed_at"
FIELD_USERS_CONSIDERED = "users_considered"
FIELD_USERS_SCORED = "users_scored"
FIELD_USERS_SKIPPED = "users_skipped"
FIELD_KNN_QUERY_COUNT = "knn_query_count"
FIELD_LAST_ERROR = "last_error"


class ClaimOutcome(Enum):
    """Result of an atomic claim attempt on one generation."""

    CLAIMED = "claimed"
    ALREADY_COMPLETE = "already_complete"
    LEASE_HELD = "lease_held"


@dataclass
class ScoringRunStats:
    """The completion counters persisted onto the generation record."""

    users_considered: int
    users_scored: int
    users_skipped: int
    knn_query_count: int


def generation_id_for(moment: datetime) -> str:
    """Deterministic UTC generation ID for the 4-hour bucket containing ``moment``,
    e.g. 2026-07-09 13:47 UTC -> "20260709T1200Z". Every caller in one ingest
    window (the scheduler run, its retries, the scoring task, a manual recovery
    call) derives the same ID, which is what makes the task name and the claim
    document collide instead of duplicating work."""
    utc_moment = moment.astimezone(UTC)
    bucket_hour = (utc_moment.hour // GENERATION_WINDOW_HOURS) * GENERATION_WINDOW_HOURS
    return f"{utc_moment:%Y%m%d}T{bucket_hour:02d}00Z"


def _generation_doc_ref(generation_id: str):
    return admin_firestore().collection(GENERATIONS_COLLECTION).document(generation_id)


def _as_aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


async def record_ingest_completed(
    generation_id: str,
    *,
    new_candidates_written: int,
    now: datetime | None = None,
) -> None:
    """Persist "this generation's ingest finished" before the scoring task is
    enqueued. Creates the record with scoring_status=pending; an ingest RETRY of
    the same generation only adds to the candidate count and never touches the
    scoring fields, so a retry can never knock a running/complete generation
    back to pending (that would defeat the duplicate suppression).

    Raises on a store failure: the ingest handler then returns non-2xx and
    Cloud Scheduler retries the (content_id-deduped, therefore cheap) ingest."""
    when = now or datetime.now(UTC)

    def _txn() -> None:
        db = admin_firestore()
        ref = _generation_doc_ref(generation_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                txn.set(ref, {
                    FIELD_GENERATION_ID: generation_id,
                    FIELD_INGEST_COMPLETED_AT: when,
                    FIELD_NEW_CANDIDATES_WRITTEN: new_candidates_written,
                    FIELD_SCORING_STATUS: STATUS_PENDING,
                    FIELD_LEASE_EXPIRES_AT: None,
                    FIELD_SCORING_STARTED_AT: None,
                    FIELD_SCORING_COMPLETED_AT: None,
                })
                return
            existing = snap.to_dict() or {}
            previous_written = int(existing.get(FIELD_NEW_CANDIDATES_WRITTEN, 0) or 0)
            txn.update(ref, {
                FIELD_INGEST_COMPLETED_AT: when,
                FIELD_NEW_CANDIDATES_WRITTEN: previous_written + new_candidates_written,
            })

        _apply(transaction)

    await asyncio.to_thread(_txn)
    logger.info("generation_store: ingest generation recorded", {
        "generation_id": generation_id,
        "new_candidates_written": new_candidates_written,
    })


async def claim_for_scoring(
    generation_id: str,
    *,
    now: datetime | None = None,
    lease_seconds: int = SCORING_LEASE_SECONDS,
) -> ClaimOutcome:
    """Atomically claim ``generation_id`` for one scoring pass.

    CLAIMED          -> this caller owns the generation; it must finish with
                        mark_scoring_complete or mark_scoring_failed.
    ALREADY_COMPLETE -> a prior delivery finished this generation; no-op.
    LEASE_HELD       -> another delivery is scoring right now (unexpired
                        lease); the caller should answer non-2xx so the retry
                        lands after the lease either completed or expired.

    A pending, failed, or lease-expired-running generation is claimable. A
    missing record is also claimable (created on the spot) so the manual
    recovery path works even when no ingest record exists for the bucket.
    Raises on a store failure (fail closed — Cloud Tasks retries)."""
    when = now or datetime.now(UTC)

    def _txn() -> tuple[ClaimOutcome, str]:
        db = admin_firestore()
        ref = _generation_doc_ref(generation_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> tuple[ClaimOutcome, str]:
            snap = ref.get(transaction=txn)
            existing = (snap.to_dict() or {}) if snap.exists else {}
            status = str(existing.get(FIELD_SCORING_STATUS, "") or "")

            if status == STATUS_COMPLETE:
                return ClaimOutcome.ALREADY_COMPLETE, status
            if status == STATUS_RUNNING:
                lease_expires = _as_aware(existing.get(FIELD_LEASE_EXPIRES_AT))
                if lease_expires is not None and lease_expires > when:
                    return ClaimOutcome.LEASE_HELD, status

            claim_fields = {
                FIELD_GENERATION_ID: generation_id,
                FIELD_SCORING_STATUS: STATUS_RUNNING,
                FIELD_LEASE_EXPIRES_AT: when + timedelta(seconds=lease_seconds),
                FIELD_SCORING_STARTED_AT: when,
            }
            if snap.exists:
                txn.update(ref, claim_fields)
            else:
                txn.set(ref, claim_fields)
            return ClaimOutcome.CLAIMED, status

        return _apply(transaction)

    outcome, previous_status = await asyncio.to_thread(_txn)
    if outcome is ClaimOutcome.CLAIMED:
        if previous_status in (STATUS_RUNNING, STATUS_FAILED):
            # A crashed worker's expired lease or a failed pass was recovered —
            # distinct from a first claim so lease recovery is visible in logs.
            logger.warn("generation_store: scoring claim RECOVERED", {
                "generation_id": generation_id,
                "previous_status": previous_status,
                "lease_seconds": lease_seconds,
            })
        else:
            logger.info("generation_store: scoring claim acquired", {
                "generation_id": generation_id,
                "lease_seconds": lease_seconds,
            })
    else:
        logger.info("generation_store: duplicate scoring delivery suppressed", {
            "generation_id": generation_id,
            "claim_outcome": outcome.value,
        })
    return outcome


async def mark_scoring_complete(
    generation_id: str,
    stats: ScoringRunStats,
    *,
    now: datetime | None = None,
) -> None:
    """Terminal success: persist the run counters and make every later delivery
    for this generation no-op. Never raises — the scoring itself succeeded, and
    failing the request here would make Cloud Tasks re-run a whole pass just to
    rewrite a status field (the orchestrator's delivered-dedup backstops the
    rare double-send that a lost complete-mark could allow)."""
    when = now or datetime.now(UTC)

    def _write() -> None:
        _generation_doc_ref(generation_id).update({
            FIELD_SCORING_STATUS: STATUS_COMPLETE,
            FIELD_SCORING_COMPLETED_AT: when,
            FIELD_LEASE_EXPIRES_AT: None,
            FIELD_USERS_CONSIDERED: stats.users_considered,
            FIELD_USERS_SCORED: stats.users_scored,
            FIELD_USERS_SKIPPED: stats.users_skipped,
            FIELD_KNN_QUERY_COUNT: stats.knn_query_count,
        })

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error(
            "generation_store: failed to mark generation complete — the record "
            "stays 'running' until its lease expires and a retry would re-score; "
            "the orchestrator's delivered-dedup is the backstop",
            {"generation_id": generation_id, "error": str(exc)},
        )


async def mark_scoring_failed(
    generation_id: str,
    *,
    error: str,
    now: datetime | None = None,
) -> None:
    """Record a scoring failure and release the lease so the Cloud Task retry
    can re-claim immediately instead of waiting out the lease. Never raises —
    the caller re-raises the original scoring error, which is what drives the
    retry; a store hiccup here only delays the re-claim until lease expiry."""
    when = now or datetime.now(UTC)

    def _write() -> None:
        _generation_doc_ref(generation_id).update({
            FIELD_SCORING_STATUS: STATUS_FAILED,
            FIELD_LEASE_EXPIRES_AT: None,
            FIELD_LAST_ERROR: error[:1000],
        })

    try:
        await asyncio.to_thread(_write)
        logger.warn("generation_store: scoring marked failed (retryable)", {
            "generation_id": generation_id,
            "error": error[:300],
            "failed_at": when.isoformat(),
        })
    except Exception as exc:
        logger.error(
            "generation_store: failed to mark generation failed — lease expiry "
            "will make it reclaimable anyway",
            {"generation_id": generation_id, "error": str(exc)},
        )
