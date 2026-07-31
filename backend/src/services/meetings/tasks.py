"""Durable outbox dispatch through Cloud Tasks for meeting synthesis.

A deliberate small duplication of task_scheduler.py's ``_enqueue`` OIDC
pattern rather than an import: engagement's TaskScheduler is in the middle of
unrelated churn, and this module needs exactly one task shape. Same queue
(juno-engagement) and same OIDC signer.

Firestore ``meeting_jobs`` and ``meeting_job_outbox`` documents are committed
with verified completion and are authoritative. Inline dispatch is an
optimization; the scheduler sweeper redelivers pending outbox rows after a
commit/dispatch crash. Cloud Tasks is only the at-least-once delivery layer.

Task names are deterministic for the durable job identity. Retry/stale-sweep
deliveries add bounded suffixes because Cloud Tasks keeps completed names
reserved for a tombstone window.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F
from . import store

_client_singleton: Any = None


def _client() -> Any:
    global _client_singleton
    if _client_singleton is None:
        from google.cloud import tasks_v2  # type: ignore

        _client_singleton = tasks_v2.CloudTasksClient()
    return _client_singleton


def enqueue_synthesis(
    uid: str,
    meeting_id: str,
    *,
    dedup_suffix: str = "",
    job_id: str = "",
) -> str:
    """Enqueue the one synthesis task for a completed capture. Synchronous
    (gRPC); call via asyncio.to_thread from async handlers. Raises on real
    enqueue failures so /complete answers 5xx and the client retries.

    ``dedup_suffix`` (empty for /complete, the attempt count for /retry) makes a
    deliberate re-run a distinct task name instead of an AlreadyExists no-op."""
    from google.api_core.exceptions import AlreadyExists  # type: ignore
    from google.cloud import tasks_v2  # type: ignore

    client = _client()
    task_id = f"meeting-synthesize-{job_id or meeting_id}"
    if dedup_suffix:
        task_id = f"{task_id}-{dedup_suffix}"
    task_path = client.task_path(
        settings.CLOUD_TASKS_PROJECT,
        settings.CLOUD_TASKS_LOCATION,
        settings.CLOUD_TASKS_QUEUE,
        task_id,
    )
    task: dict[str, Any] = {
        "name": task_path,
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{settings.BACKEND_INTERNAL_URL}/internal/meetings/synthesize",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "user_id": uid,
                    "meeting_id": meeting_id,
                    "job_id": job_id,
                }
            ).encode(),
            "oidc_token": {
                "service_account_email": settings.SCHEDULER_SA_EMAIL,
                "audience": settings.BACKEND_INTERNAL_URL,
            },
        },
    }

    queue_path = client.queue_path(
        settings.CLOUD_TASKS_PROJECT,
        settings.CLOUD_TASKS_LOCATION,
        settings.CLOUD_TASKS_QUEUE,
    )
    try:
        created = client.create_task(parent=queue_path, task=task)
        task_name = created.name
    except AlreadyExists:
        task_name = task_path
        logger.info(
            "meetings.tasks: duplicate synthesis enqueue suppressed",
            {
                "user_id": uid,
                "meeting_id": meeting_id,
            },
        )
        return task_name

    logger.info(
        "meetings.tasks: synthesis enqueued",
        {
            "user_id": uid,
            "meeting_id": meeting_id,
            "task_name": task_name,
        },
    )
    return task_name


async def dispatch_job(uid: str, job_id: str) -> bool:
    """Dispatch one durable Firestore job, then record the delivery hint.

    Firestore remains authoritative. A crash after Cloud Tasks accepts the
    request but before this transaction commits is safe because the task name
    is deterministic and AlreadyExists reconciles the retry.
    """
    now = datetime.now(UTC)

    def _read() -> tuple[dict[str, Any], dict[str, Any]]:
        job = store._jobs_ref(uid).document(job_id).get()
        outbox = store._outbox_ref(uid).document(job_id).get()
        return job.to_dict() or {}, outbox.to_dict() or {}

    job, outbox = await asyncio.to_thread(_read)
    if not job or not outbox or job.get("state") in (F.JOB_COMPLETE, F.JOB_BLOCKED):
        return False
    stale_dispatch = (
        outbox.get("state") == "dispatched"
        and str(outbox.get("last_dispatched_at", "")) <= (now - timedelta(minutes=10)).isoformat()
        and job.get("state") in (F.JOB_PENDING, F.JOB_DISPATCHED)
    )
    if outbox.get("state") == "dispatched" and not stale_dispatch:
        return True
    task_name = await asyncio.to_thread(
        enqueue_synthesis,
        uid,
        str(job["meeting_id"]),
        dedup_suffix=(
            f"a{int(job.get('job_attempt', 0))}"
            if outbox.get("state") == "retry"
            else f"s{int(outbox.get('dispatch_attempts', 0))}"
            if stale_dispatch
            else ""
        ),
        job_id=job_id,
    )

    def _commit() -> bool:
        db = admin_firestore()
        job_ref = store._jobs_ref(uid).document(job_id)
        outbox_ref = store._outbox_ref(uid).document(job_id)
        meeting_ref = store._meetings_ref(uid).document(str(job["meeting_id"]))
        txn = db.transaction()

        @store.gcloud_firestore.transactional
        def _execute(transaction) -> bool:
            job_snap = job_ref.get(transaction=transaction)
            outbox_snap = outbox_ref.get(transaction=transaction)
            meeting_snap = meeting_ref.get(transaction=transaction)
            current_job = job_snap.to_dict() or {}
            current_outbox = outbox_snap.to_dict() or {}
            meeting = meeting_snap.to_dict() or {}
            if (
                not current_job
                or not current_outbox
                or current_job.get("state") in (F.JOB_COMPLETE, F.JOB_BLOCKED)
            ):
                return False
            recorded_at = datetime.now(UTC).isoformat()
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            transaction.update(
                job_ref,
                {
                    "dispatch_state": "dispatched",
                    "task_name": task_name,
                    "last_dispatched_at": recorded_at,
                    "updated_at": recorded_at,
                },
            )
            transaction.update(
                outbox_ref,
                {
                    "state": "dispatched",
                    "task_name": task_name,
                    "dispatch_attempts": store.gcloud_firestore.Increment(1),
                    "last_dispatched_at": recorded_at,
                    "updated_at": recorded_at,
                },
            )
            transaction.update(meeting_ref, {F.AUDIT_SEQUENCE: sequence})
            store._audit_event(
                transaction,
                uid=uid,
                meeting_id=str(job["meeting_id"]),
                sequence=sequence,
                event_type="outbox_dispatched",
                occurred_at=recorded_at,
                capture_run_id=str(job.get(F.CAPTURE_RUN_ID, "")),
                capture_fence=int(job.get(F.CAPTURE_FENCE, 0)),
                job_id=job_id,
                prior_state=current_outbox.get("state", ""),
                next_state="dispatched",
                reason_code="cloud_tasks_accepted",
                correlation_id=str(job.get("correlation_id", "")),
            )
            return True

        return _execute(txn)

    return await asyncio.to_thread(_commit)


async def dispatch_pending(*, limit: int = 50) -> dict[str, int]:
    """Sweep due outbox rows. Zero results are logged distinctly for operations."""
    now = datetime.now(UTC)

    def _query() -> list[dict[str, Any]]:
        query = (
            admin_firestore()
            .collection_group(F.JOB_OUTBOX_SUBCOLLECTION)
            .where("dispatch_due_at", "<=", now.isoformat())
            .limit(limit)
        )
        rows: list[dict[str, Any]] = []
        for snap in query.stream():
            row = snap.to_dict() or {}
            if row.get("state") in ("pending", "retry", "dispatched"):
                rows.append(row)
        return rows

    rows = await asyncio.to_thread(_query)
    if not rows:
        logger.info(
            "meetings.tasks: outbox sweep empty",
            {
                "query": "meeting_job_outbox.dispatch_due_at",
            },
        )
        return {"scanned": 0, "dispatched": 0, "failed": 0}
    dispatched = 0
    failed = 0
    for row in rows:
        try:
            if await dispatch_job(str(row["user_id"]), str(row["job_id"])):
                dispatched += 1
        except Exception as exc:
            failed += 1
            logger.error(
                "meetings.tasks: durable dispatch failed",
                {
                    "meeting_id": row.get("meeting_id"),
                    "job_id": row.get("job_id"),
                    "correlation_id": row.get("correlation_id"),
                    "error_code": "outbox_dispatch_failed",
                    "error": str(exc),
                },
            )
            # The row remains authoritative and due; the next sweep retries.
    return {"scanned": len(rows), "dispatched": dispatched, "failed": failed}
