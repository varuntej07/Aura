"""Explicit deletion of one run's exact subtree, resumable across crashes.

This exists because of one Firestore fact: **native TTL does not recursively delete
subcollections.** Expiring ``research_runs/{run_id}`` would delete the parent and orphan
its ledger, coord docs, stages, plans, sources, claims and audit events, which would
then sit unreferenced and un-expiring until someone noticed the bill. Every run-owned
document therefore carries the same ``expires_at`` for the TTL path, AND explicit user
deletion goes through this receipt rather than trusting TTL timing.

The user-visible contract is immediate: the delete request hides the run and cancels it
in one transaction, so it disappears at once and never reappears after a restart. The
actual draining is bounded background work behind that.

Resumability is the whole design. A receipt records WHICH collection it was draining and
the last document id it deleted, so a retry after a crash continues from that cursor
instead of restarting the subtree. On a run with sixty sources and a hundred claims,
restarting each time would mean a deletion that never finishes inside its bounded budget.

The walk covers more than the run's own subtree. ``research_jobs`` and
``research_job_outbox`` hang off the USER document rather than the run, so that the
outbox sweep can reach every user in one collection-group query, but their rows are
still one run's data: a job row carries the stage payload, including discovered URLs,
and stays redispatchable. Draining only the run's subcollections left those behind, so a
"deleted" run kept its URLs on disk and could still hand a stage to a worker. Both are
filtered by ``job_run_id``, because unlike the run-owned collections they also hold
other runs' rows.

The global ``research_domain_classes`` cache is deliberately NOT touched: it holds no
query, uid or run id, and it expires independently after 30 days.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore as gcloud_firestore

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F
from .store import (
    _audit_event,
    _deletions_ref,
    _run_ref,
    _sub_ref,
    _txn_create,
)

# Documents deleted per batch. Well under Firestore's 500-write commit ceiling, leaving
# head-room for the receipt update that rides along with each batch.
DELETE_BATCH = 200
# Batches per drain call. Bounds one invocation's wall clock so the sweeper stays
# predictable; whatever is left resumes on the next pass from the recorded cursor.
MAX_BATCHES_PER_DRAIN = 10


@dataclass(frozen=True)
class DeletionProgress:
    """One drain invocation's outcome."""

    run_id: str
    state: str
    deleted: int = 0
    collection_index: int = 0
    complete: bool = False
    resumed: bool = False


async def request_deletion(
    uid: str, run_id: str, *, correlation_id: str = ""
) -> tuple[bool, str]:
    """Hide and cancel the run, then create its deletion receipt. One transaction.

    Hiding and cancelling together matters: cancellation stops any in-flight stage from
    spending more, and hiding makes the run vanish from every read path immediately,
    long before the subtree is actually drained.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    def _run() -> tuple[bool, str]:
        db = admin_firestore()
        run_ref = _run_ref(uid, run_id)
        receipt_ref = _deletions_ref(uid).document(run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> tuple[bool, str]:
            run_snap = run_ref.get(transaction=txn)
            receipt_snap = receipt_ref.get(transaction=txn)
            if not run_snap.exists:
                # Already fully drained. Idempotent: the user asked for gone, it is gone.
                return True, F.DELETION_DONE
            if receipt_snap.exists:
                existing = receipt_snap.to_dict() or {}
                return True, str(existing.get(F.DELETION_RECEIPT_STATE, F.DELETION_PENDING))

            run = run_snap.to_dict() or {}
            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            expires_at = str(run.get(F.EXPIRES_AT, ""))
            txn.update(
                run_ref,
                {
                    F.HIDDEN_AT: now_iso,
                    F.DELETION_STATE: F.DELETION_REQUESTED,
                    F.CANCEL_REQUESTED_AT: run.get(F.CANCEL_REQUESTED_AT) or now_iso,
                    F.STATE_REVISION: gcloud_firestore.Increment(1),
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now_iso,
                },
            )
            _txn_create(
                txn,
                receipt_ref,
                {
                    F.RUN_ID: run_id,
                    F.DELETION_RECEIPT_STATE: F.DELETION_PENDING,
                    F.DELETION_COLLECTION_INDEX: 0,
                    F.DELETION_CURSOR: "",
                    F.DELETION_DELETED_COUNTS: {},
                    F.DELETION_ATTEMPTS: 0,
                    F.DELETION_REQUESTED_AT: now_iso,
                    F.DELETION_COMPLETED_AT: "",
                    F.CORRELATION_ID: correlation_id,
                    # The receipt outlives the run itself, as proof the subtree really
                    # was drained, then TTL reaps it.
                    F.EXPIRES_AT: (
                        now + timedelta(days=F.DELETION_RECEIPT_TTL_DAYS)
                    ).isoformat(),
                },
            )
            _audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=sequence,
                event_type="deletion_requested",
                occurred_at=now_iso,
                expires_at=expires_at,
                prior_state=str(run.get(F.STATE, "")),
                next_state=F.DELETION_REQUESTED,
                reason_code="user_requested",
                correlation_id=correlation_id,
            )
            return True, F.DELETION_PENDING

        return _execute(transaction)

    return await asyncio.to_thread(_run)


def _collection_for(uid: str, run_id: str, name: str) -> tuple[Any, bool]:
    """Resolve one deletion target to its collection, and whether it needs a run filter.

    Run-owned subcollections live under the run document, so every document in them
    belongs to this run by construction. The two user-level collections do not: they hold
    every run's jobs for that user, so they MUST be filtered by run_id or a deletion
    would take out another run's pending work.
    """
    if name in F.RUN_SCOPED_USER_COLLECTIONS:
        return (
            admin_firestore()
            .collection(F.PARENT_COLLECTION)
            .document(uid)
            .collection(name),
            True,
        )
    return _sub_ref(uid, run_id, name), False


def _delete_batch(uid: str, run_id: str, subcollection: str, cursor: str) -> tuple[int, str]:
    """Delete up to DELETE_BATCH docs from one collection, after ``cursor``.

    Ordering by document id and resuming after the last deleted id is what makes this
    resumable without a separate index. Returns (deleted, new_cursor); an empty cursor
    back means the collection is drained.
    """
    db = admin_firestore()
    if subcollection == F.PROJECT_RECEIPTS_DELETION_TARGET:
        query = (
            db.collection_group(F.PROJECT_RECEIPTS_SUBCOLLECTION)
            .where(F.RECEIPT_USER_ID, "==", uid)
            .where(F.RECEIPT_RUN_ID, "==", run_id)
            .limit(DELETE_BATCH)
        )
        snaps = list(query.stream())
        if not snaps:
            return 0, ""
        now_iso = datetime.now(UTC).isoformat()
        deleted = 0
        for snap in snaps:
            receipt_ref = snap.reference
            budget_ref = receipt_ref.parent.parent
            transaction = db.transaction()

            @gcloud_firestore.transactional
            def _remove_receipt(txn: Any) -> bool:
                current_snap = receipt_ref.get(transaction=txn)
                if not current_snap.exists:
                    return False
                current = current_snap.to_dict() or {}
                if (
                    current.get(F.RECEIPT_USER_ID) != uid
                    or current.get(F.RECEIPT_RUN_ID) != run_id
                ):
                    return False
                budget_snap = None
                if current.get(F.RECEIPT_STATE) == F.RECEIPT_RESERVED:
                    budget_snap = budget_ref.get(transaction=txn)
                if budget_snap is not None:
                    budget = budget_snap.to_dict() or {} if budget_snap.exists else {}
                    estimate = int(current.get(F.RECEIPT_ESTIMATE_MICROUSD, 0))
                    txn.set(
                        budget_ref,
                        {
                            F.PROJECT_RESERVED_MICROUSD: max(
                                0,
                                int(budget.get(F.PROJECT_RESERVED_MICROUSD, 0))
                                - estimate,
                            ),
                            F.PROJECT_ACTUAL_MICROUSD: int(
                                budget.get(F.PROJECT_ACTUAL_MICROUSD, 0)
                            )
                            + estimate,
                            F.UPDATED_AT: now_iso,
                        },
                        merge=True,
                    )
                txn.delete(receipt_ref)
                return True

            if _remove_receipt(transaction):
                deleted += 1
        return deleted, ("more" if len(snaps) == DELETE_BATCH else "")
    collection, needs_run_filter = _collection_for(uid, run_id, subcollection)
    query = collection
    if needs_run_filter:
        # Equality plus __name__ ordering is served by the automatic single-field index,
        # which already carries __name__ as its tiebreak, so this needs no composite one.
        query = query.where(F.JOB_RUN_ID, "==", run_id)
    query = query.order_by("__name__").limit(DELETE_BATCH)
    if cursor:
        query = query.start_after({"__name__": collection.document(cursor)})

    snaps = list(query.stream())
    if not snaps:
        return 0, ""

    batch = db.batch()
    last_id = cursor
    for snap in snaps:
        batch.delete(snap.reference)
        last_id = snap.id
    batch.commit()
    # A short page means we reached the end of this collection.
    return len(snaps), ("" if len(snaps) < DELETE_BATCH else last_id)


async def drain_deletion(
    uid: str,
    run_id: str,
    *,
    max_batches: int = MAX_BATCHES_PER_DRAIN,
) -> DeletionProgress:
    """Advance one deletion receipt. Safe to call repeatedly; resumes from its cursor.

    The final step deletes the run document itself, and only after every owned
    collection has been walked to empty. Deleting the parent first would orphan whatever
    remained, which is precisely the failure this whole module exists to prevent.
    """

    def _read_receipt() -> dict[str, Any] | None:
        snap = _deletions_ref(uid).document(run_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    receipt = await asyncio.to_thread(_read_receipt)
    if receipt is None:
        return DeletionProgress(run_id=run_id, state="missing")
    if receipt.get(F.DELETION_RECEIPT_STATE) == F.DELETION_DONE:
        # Idempotent. A redelivered delete job must not re-walk a drained subtree.
        return DeletionProgress(run_id=run_id, state=F.DELETION_DONE, complete=True)

    index = int(receipt.get(F.DELETION_COLLECTION_INDEX, 0))
    cursor = str(receipt.get(F.DELETION_CURSOR, ""))
    counts = dict(receipt.get(F.DELETION_DELETED_COUNTS) or {})
    resumed = bool(cursor or index)
    deleted_now = 0
    batches = 0

    while index < len(F.DELETION_COLLECTIONS) and batches < max_batches:
        subcollection = F.DELETION_COLLECTIONS[index]
        try:
            deleted, cursor = await asyncio.to_thread(
                _delete_batch, uid, run_id, subcollection, cursor
            )
        except Exception as exc:
            # Leave the receipt exactly where it was. The next pass resumes from the
            # same cursor rather than skipping documents it never actually deleted.
            logger.error(
                "research.deletion: batch failed, will resume",
                {"run_id": run_id, "collection": subcollection, "error": str(exc),
                 "error_code": "research_deletion_batch_failed"},
            )
            await _save_progress(uid, run_id, index, cursor, counts, complete=False)
            return DeletionProgress(
                run_id=run_id,
                state=F.DELETION_RUNNING,
                deleted=deleted_now,
                collection_index=index,
                resumed=resumed,
            )

        batches += 1
        deleted_now += deleted
        counts[subcollection] = int(counts.get(subcollection, 0)) + deleted
        if not cursor:
            index += 1  # this collection is drained; move to the next

    if index >= len(F.DELETION_COLLECTIONS):
        # _finish re-reads every collection and may find a document written by an
        # in-flight stage after that collection was drained. When it does it re-queues
        # itself, and reporting complete=True here would tell the caller the subtree was
        # gone while _finish had just decided the opposite.
        finished = await _finish(uid, run_id, counts)
        return DeletionProgress(
            run_id=run_id,
            state=F.DELETION_DONE if finished else F.DELETION_RUNNING,
            deleted=deleted_now,
            collection_index=index,
            complete=finished,
            resumed=resumed,
        )

    await _save_progress(uid, run_id, index, cursor, counts, complete=False)
    return DeletionProgress(
        run_id=run_id,
        state=F.DELETION_RUNNING,
        deleted=deleted_now,
        collection_index=index,
        resumed=resumed,
    )


async def _save_progress(
    uid: str,
    run_id: str,
    index: int,
    cursor: str,
    counts: dict[str, Any],
    *,
    complete: bool,
) -> None:
    now_iso = datetime.now(UTC).isoformat()

    def _run() -> None:
        _deletions_ref(uid).document(run_id).set(
            {
                F.DELETION_RECEIPT_STATE: (
                    F.DELETION_DONE if complete else F.DELETION_RUNNING
                ),
                F.DELETION_COLLECTION_INDEX: index,
                F.DELETION_CURSOR: cursor,
                F.DELETION_DELETED_COUNTS: counts,
                F.DELETION_ATTEMPTS: gcloud_firestore.Increment(1),
                F.UPDATED_AT: now_iso,
            },
            merge=True,
        )

    await asyncio.to_thread(_run)


async def _finish(uid: str, run_id: str, counts: dict[str, Any]) -> bool:
    """Verify every owned collection is empty, then delete the run and close the receipt.

    Returns True only when the run document was actually removed. False means leftovers
    were found and the receipt was re-queued, and the caller MUST NOT report the deletion
    as complete: doing so told the user their data was gone at the exact moment this
    function had decided it was not.

    The verification pass is not paranoia. A document written by an in-flight stage
    AFTER its collection was drained would otherwise survive its own parent, so the run
    document is only removed once a fresh read of every collection comes back empty.
    """
    now_iso = datetime.now(UTC).isoformat()

    def _run() -> bool:
        leftovers: list[str] = []
        for subcollection in F.DELETION_COLLECTIONS:
            if subcollection == F.PROJECT_RECEIPTS_DELETION_TARGET:
                receipts = (
                    admin_firestore()
                    .collection_group(F.PROJECT_RECEIPTS_SUBCOLLECTION)
                    .where(F.RECEIPT_USER_ID, "==", uid)
                    .where(F.RECEIPT_RUN_ID, "==", run_id)
                    .limit(1)
                )
                if list(receipts.stream()):
                    leftovers.append(subcollection)
                continue
            collection, needs_run_filter = _collection_for(uid, run_id, subcollection)
            query = collection
            if needs_run_filter:
                query = query.where(F.JOB_RUN_ID, "==", run_id)
            if list(query.limit(1).stream()):
                leftovers.append(subcollection)
        if leftovers:
            # Reset to the first offending collection and let the next pass finish it.
            _deletions_ref(uid).document(run_id).set(
                {
                    F.DELETION_RECEIPT_STATE: F.DELETION_RUNNING,
                    F.DELETION_COLLECTION_INDEX: F.DELETION_COLLECTIONS.index(
                        leftovers[0]
                    ),
                    F.DELETION_CURSOR: "",
                    F.UPDATED_AT: now_iso,
                },
                merge=True,
            )
            logger.warn(
                "research.deletion: subtree not empty, re-queuing",
                {"run_id": run_id, "collections": leftovers},
            )
            return False

        _run_ref(uid, run_id).delete()
        _deletions_ref(uid).document(run_id).set(
            {
                F.DELETION_RECEIPT_STATE: F.DELETION_DONE,
                F.DELETION_DELETED_COUNTS: counts,
                F.DELETION_COMPLETED_AT: now_iso,
                F.DELETION_CURSOR: "",
                F.UPDATED_AT: now_iso,
            },
            merge=True,
        )
        return True

    finished = await asyncio.to_thread(_run)
    if finished:
        logger.info(
            "research.deletion: run subtree drained",
            {"run_id": run_id, "counts": counts, "metric": "research_deletion_complete"},
        )
    return finished


async def pending_deletions(*, limit: int = 20) -> list[tuple[str, str]]:
    """Receipts still needing work, as (uid, run_id). Used by the sweeper."""

    def _run() -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        query = (
            admin_firestore()
            .collection_group(F.DELETIONS_SUBCOLLECTION)
            .where(F.DELETION_RECEIPT_STATE, "in", [F.DELETION_PENDING, F.DELETION_RUNNING])
            .limit(limit)
        )
        for snap in query.stream():
            data = snap.to_dict() or {}
            run_id = str(data.get(F.RUN_ID, ""))
            # users/{uid}/research_deletions/{run_id}
            uid = ""
            try:
                uid = snap.reference.parent.parent.id
            except Exception:
                uid = ""
            if uid and run_id:
                rows.append((uid, run_id))
        return rows

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.error(
            "research.deletion: pending query failed",
            {"error": str(exc), "error_code": "research_deletion_query_failed"},
        )
        return []
