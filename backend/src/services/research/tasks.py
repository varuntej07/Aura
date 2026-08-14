"""The outbox and its dispatcher seam. Firestore is authoritative; delivery is a hint.

An outbox row is durable INTENT: "this stage exists and nobody has delivered it yet".
It is created inside the same transaction that advanced the run, so the commit-then-
crash window that would otherwise lose a stage forever cannot exist. Whatever happens to
the delivery attempt, the row survives and the sweeper redelivers it.

**Phase two deliberately does not dispatch anything.** There is no ``google.cloud.
tasks_v2`` import here, no queue name, and no OIDC token. Phase three provisions the
``juno-research`` queue with a bounded rate, an explicit dispatch deadline and a retry
policy, and only then is a real dispatcher installed through ``set_dispatcher``. Until
that happens rows accumulate as pending intent and ``dispatch_pending`` reports them as
deferred, which is the correct behaviour for an engine that is not yet allowed to run.

Task naming is still computed and persisted now, because it encodes a constraint that is
easy to lose later: **Cloud Tasks reserves a completed task's name for a tombstone
window of roughly an hour.** A legitimate retry after a crash MUST mint a new name or
the enqueue is silently swallowed and the stage never runs again. That is what the
``a{attempt}-d{dispatch}`` suffix is for, and it is the same reason the meetings engine
threads a ``dedup_suffix``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from google.cloud import firestore as gcloud_firestore

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F
from . import store


def task_name_for(stage_id: str, *, attempt: int, dispatch_attempts: int) -> str:
    """The deterministic Cloud Task name for one delivery of one stage.

    Deterministic so a duplicate dispatch collides with AlreadyExists and is treated as
    success. Attempt-and-dispatch suffixed so a legitimate retry escapes the tombstone
    window rather than being silently dropped.
    """
    return f"research-{stage_id}-a{attempt}-d{dispatch_attempts}"


@runtime_checkable
class Dispatcher(Protocol):
    """Delivers one stage to wherever stages actually run.

    Narrow on purpose. Everything Cloud-Tasks-shaped (queue, OIDC, deadline, retry
    config) lives behind this, so swapping the execution substrate touches one class.
    """

    async def dispatch(
        self,
        uid: str,
        run_id: str,
        stage_id: str,
        *,
        task_name: str,
        schedule_time: datetime | None = None,
    ) -> str:
        """Return the delivered task name, or "" when delivery did not happen.

        ``schedule_time`` is when the task should FIRE, not when it was enqueued. Without
        it a retry could only be delivered by firing immediately, which throws away the
        backoff, or by leaving it for the next recovery sweep, which throws away the run.
        """
        ...


class NullDispatcher:
    """The phase-two default. Records that delivery was declined, delivers nothing.

    Not a silent no-op: it returns "" so ``dispatch_pending`` counts the row as deferred
    and leaves it due. A row left due is recoverable; a row marked dispatched that never
    ran is not, which is why this must never claim success.
    """

    available = False

    async def dispatch(
        self,
        uid: str,
        run_id: str,
        stage_id: str,
        *,
        task_name: str,
        schedule_time: datetime | None = None,
    ) -> str:
        return ""


_dispatcher: Dispatcher = NullDispatcher()


def set_dispatcher(dispatcher: Dispatcher) -> None:
    """Install the real dispatcher. Called by phase three, never at import time."""
    global _dispatcher
    _dispatcher = dispatcher


def get_dispatcher() -> Dispatcher:
    return _dispatcher


@dataclass(frozen=True)
class DispatchReport:
    scanned: int = 0
    dispatched: int = 0
    deferred: int = 0
    skipped: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "dispatched": self.dispatched,
            "deferred": self.deferred,
            "skipped": self.skipped,
            "failed": self.failed,
        }


def _due_at(due_iso: str, now: datetime) -> datetime | None:
    """The future instant this row is due, or None when it is due already.

    None rather than ``now`` on purpose: a task with no schedule_time fires as soon as the
    queue can take it, which is what an already-due row wants, and it keeps the common
    path from carrying a timestamp that has to be right to the second.
    """
    if not due_iso or due_iso == store.DISPATCH_NEVER:
        return None
    try:
        due = datetime.fromisoformat(due_iso)
    except ValueError:
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return due if due > now else None


def _is_stale_dispatch(outbox: dict[str, Any], job: dict[str, Any], now: datetime) -> bool:
    """A row that was marked dispatched but plainly never ran.

    Ten minutes matches the meetings engine's predicate. The point is to rescue the
    window between "Cloud Tasks accepted the task" and "a worker actually leased it",
    which is exactly where a queue outage or a dropped delivery hides.
    """
    cutoff = (now - timedelta(minutes=F.STALE_DISPATCH_MINUTES)).isoformat()
    return (
        outbox.get(F.OUTBOX_STATE) == F.OUTBOX_DISPATCHED
        and str(outbox.get(F.LAST_DISPATCHED_AT, "")) <= cutoff
        and job.get(F.JOB_STATE) in (F.STAGE_PENDING, F.STAGE_DISPATCHED)
    )


async def dispatch_job(uid: str, stage_id: str) -> str:
    """Deliver one stage, then record the delivery. Returns the disposition.

    Enqueue happens BEFORE the commit, matching the meetings engine. A crash between the
    two is reconciled by the deterministic task name: the redelivery collides with
    AlreadyExists and is treated as success rather than creating a second task.
    """
    now = datetime.now(UTC)

    def _read() -> tuple[dict[str, Any], dict[str, Any]]:
        job_snap = store._jobs_ref(uid).document(stage_id).get()
        outbox_snap = store._outbox_ref(uid).document(stage_id).get()
        return (
            (job_snap.to_dict() or {}) if job_snap.exists else {},
            (outbox_snap.to_dict() or {}) if outbox_snap.exists else {},
        )

    job, outbox = await asyncio.to_thread(_read)
    if not job or not outbox:
        return "missing"
    if job.get(F.JOB_STATE) in (F.STAGE_DONE, F.STAGE_ABANDONED, F.STAGE_FAILED):
        return "skipped"

    stale = _is_stale_dispatch(outbox, job, now)
    if outbox.get(F.OUTBOX_STATE) == F.OUTBOX_DISPATCHED and not stale:
        return "skipped"  # a fresh dispatch is already in flight

    dispatch_attempts = int(outbox.get(F.DISPATCH_ATTEMPTS, 0))
    task_name = task_name_for(
        stage_id,
        attempt=int(job.get(F.STAGE_ATTEMPT, 0)),
        dispatch_attempts=dispatch_attempts,
    )
    run_id = str(job.get(F.JOB_RUN_ID, ""))

    # A row can be delivered BEFORE it is due: a retry is committed with a jittered due
    # time and handed straight to this function, precisely so it does not have to wait for
    # the next five-minute sweep. Honouring the due date as the task's schedule_time is
    # what keeps that immediate hand-off from also throwing away the backoff. A row whose
    # due date has already passed schedules for now, which is the sweep's own case.
    schedule_time = _due_at(str(outbox.get(F.DISPATCH_DUE_AT, "")), now)

    delivered = await _dispatcher.dispatch(
        uid, run_id, stage_id, task_name=task_name, schedule_time=schedule_time
    )
    if not delivered:
        # No dispatcher installed, or delivery declined. Leave the row DUE so the next
        # sweep retries it. Marking it dispatched here would strand the stage forever.
        return "deferred"

    now_iso = datetime.now(UTC).isoformat()

    def _commit() -> str:
        db = admin_firestore()
        job_ref = store._jobs_ref(uid).document(stage_id)
        outbox_ref = store._outbox_ref(uid).document(stage_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> str:
            job_snap = job_ref.get(transaction=txn)
            outbox_snap = outbox_ref.get(transaction=txn)
            if not job_snap.exists or not outbox_snap.exists:
                return "missing"
            current = job_snap.to_dict() or {}
            if current.get(F.JOB_STATE) in (F.STAGE_DONE, F.STAGE_ABANDONED, F.STAGE_FAILED):
                return "skipped"
            txn.update(
                job_ref,
                {
                    F.JOB_STATE: F.STAGE_DISPATCHED,
                    F.TASK_NAME: delivered,
                    F.LAST_DISPATCHED_AT: now_iso,
                    F.UPDATED_AT: now_iso,
                },
            )
            txn.update(
                outbox_ref,
                {
                    F.OUTBOX_STATE: F.OUTBOX_DISPATCHED,
                    F.TASK_NAME: delivered,
                    F.DISPATCH_ATTEMPTS: gcloud_firestore.Increment(1),
                    F.LAST_DISPATCHED_AT: now_iso,
                    F.UPDATED_AT: now_iso,
                },
            )
            # The task name is mirrored onto the stage doc so cancellation can delete a
            # not-yet-fired task, which is what actually saves money on a wide fan-out.
            txn.update(
                store._stages_ref(uid, run_id).document(stage_id),
                {F.TASK_NAME: delivered, F.UPDATED_AT: now_iso},
            )
            return "dispatched"

        return _execute(transaction)

    return await asyncio.to_thread(_commit)


async def dispatch_pending(*, limit: int = 50) -> DispatchReport:
    """Redeliver every outbox row that is due. The commit-then-crash recovery path.

    A collection-group query, so one pass covers every user without fanning out per uid.
    Requires the ``research_job_outbox`` / ``dispatch_due_at`` collection-group index
    that phase three deploys.
    """
    now_iso = datetime.now(UTC).isoformat()

    def _query() -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        query = (
            admin_firestore()
            .collection_group(F.JOB_OUTBOX_SUBCOLLECTION)
            .where(F.DISPATCH_DUE_AT, "<=", now_iso)
            # Oldest due first. Firestore orders by the inequality field implicitly, but
            # stating it makes the starvation property explicit: the limit now truncates
            # the NEWEST pending work, never the oldest, and a completed row cannot be in
            # this range at all because store retires it to DISPATCH_NEVER.
            .order_by(F.DISPATCH_DUE_AT)
            .limit(limit)
        )
        for snap in query.stream():
            data = snap.to_dict() or {}
            # Defence for rows written before terminal retirement existed. Those still
            # carry a due date in the past and would otherwise consume the limit forever.
            if data.get(F.OUTBOX_STATE) not in F.OUTBOX_DISPATCHABLE_STATES:
                continue
            uid = str(data.get(F.JOB_USER_ID, ""))
            stage_id = str(data.get(F.JOB_ID, ""))
            if uid and stage_id:
                rows.append((uid, stage_id))
        return rows

    try:
        rows = await asyncio.to_thread(_query)
    except Exception as exc:
        logger.error(
            "research.tasks: outbox query failed",
            {"error": str(exc), "error_code": "research_outbox_query_failed"},
        )
        return DispatchReport(failed=1)

    scanned = dispatched = deferred = skipped = failed = 0
    for uid, stage_id in rows:
        scanned += 1
        try:
            outcome = await dispatch_job(uid, stage_id)
        except Exception as exc:
            # One bad row must never abort the sweep for everyone else. Leaving it due
            # is the recovery: the next pass tries again.
            failed += 1
            logger.error(
                "research.tasks: outbox dispatch failed",
                {"stage_id": stage_id, "error": str(exc),
                 "error_code": "research_outbox_dispatch_failed"},
            )
            continue
        if outcome == "dispatched":
            dispatched += 1
        elif outcome == "deferred":
            deferred += 1
        else:
            skipped += 1

    report = DispatchReport(
        scanned=scanned,
        dispatched=dispatched,
        deferred=deferred,
        skipped=skipped,
        failed=failed,
    )
    if deferred:
        # Loud on purpose. Deferred rows mean durable work exists that nothing is
        # delivering, which is expected in phase two and an incident after phase three.
        logger.info(
            "research.tasks: outbox rows deferred, no dispatcher installed",
            {**report.as_dict(), "metric": "research_outbox_dispatch"},
        )
    return report
