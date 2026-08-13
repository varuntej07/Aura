"""Bounded Meeting V2 reconciliation: metrics, repair, and alert hooks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F
from . import notifications, store, tasks


async def reconciliation_snapshot(*, limit: int = 200) -> dict[str, Any]:
    now = datetime.now(UTC)
    stale_capture_before = (now - timedelta(minutes=5)).isoformat()
    stall_deadline = (now - timedelta(minutes=F.STALL_DEADLINE_MINUTES)).isoformat()

    def _read() -> dict[str, Any]:
        db = admin_firestore()
        meetings = []
        for snap in (
            db.collection_group(F.SUBCOLLECTION)
            .where(F.PROTOCOL_VERSION, "==", 2)
            .limit(limit)
            .stream()
        ):
            row = snap.to_dict() or {}
            row["meeting_id"] = snap.id
            # A collection-group hit knows its owner only through its path:
            # users/{uid}/{SUBCOLLECTION}/{meeting_id}. Repair needs the uid.
            parent = snap.reference.parent.parent
            row["_uid"] = parent.id if parent is not None else ""
            meetings.append(row)
        jobs = [
            snap.to_dict() or {}
            for snap in db.collection_group(F.JOBS_SUBCOLLECTION).limit(limit).stream()
        ]
        stranded_runs = [
            snap.to_dict() or {}
            for snap in (
                db.collection_group(F.CAPTURE_RUNS_SUBCOLLECTION)
                .where(F.UPDATED_AT, "<=", stale_capture_before)
                .limit(limit)
                .stream()
            )
            if (snap.to_dict() or {}).get(F.CAPTURE_RUN_STATE) == F.CAPTURE_RUN_FINALIZED
        ]
        return {"meetings": meetings, "jobs": jobs, "stranded_runs": stranded_runs}

    data = await asyncio.to_thread(_read)
    meetings = data["meetings"]
    jobs = data["jobs"]
    job_meetings = {job.get("meeting_id") for job in jobs}
    metrics = {
        "captures_finalized_without_completion": len(data["stranded_runs"]),
        "uploaded_without_durable_job": sum(
            1
            for meeting in meetings
            if meeting.get(F.STATUS) == F.STATUS_UPLOADED
            and meeting.get("meeting_id") not in job_meetings
        ),
        "jobs_pending_not_dispatched": sum(
            1
            for job in jobs
            if job.get("state") in (F.JOB_PENDING, F.JOB_RETRY)
            and job.get("dispatch_state") != "dispatched"
        ),
        "worker_leases_expired": sum(
            1
            for job in jobs
            if job.get("state") == F.JOB_LEASED
            and str(job.get("lease_expires_at", "")) <= now.isoformat()
        ),
        "provider_output_failures": sum(
            1
            for job in jobs
            if job.get("last_error_code") in (F.FAIL_PROVIDER_EMPTY, F.FAIL_PROVIDER_MALFORMED)
        ),
        "quality_failures": sum(
            1
            for meeting in meetings
            if meeting.get(F.QUALITY_OUTCOME) in ("needs_attention", "retry_transcription")
        ),
        "ready_without_immutable_evidence": sum(
            1
            for meeting in meetings
            if meeting.get(F.STATUS) == F.STATUS_READY
            and (
                meeting.get(F.QUALITY_OUTCOME) not in ("quality_passed", "verified_silence")
                or not (meeting.get(F.ARTIFACTS) or {}).get("canonical")
                or not (meeting.get(F.ARTIFACTS) or {}).get("quality_report")
            )
        ),
        "integrity_conflicts": sum(
            1
            for meeting in meetings
            if meeting.get(F.FAILURE_CODE)
            in (
                F.FAIL_STALE_CAPTURE_FENCE,
                F.FAIL_IMMUTABLE_OBJECT_CONFLICT,
                F.FAIL_SEGMENT_IDENTITY_CONFLICT,
                F.FAIL_COMPLETION_CONFLICT,
                F.FAIL_MANIFEST_INTEGRITY,
            )
        ),
    }
    repairs = await _repair(meetings, jobs, stall_deadline=stall_deadline)
    alerts = {key: value for key, value in metrics.items() if value}
    logger.info(
        "meetings.operations: reconciliation snapshot",
        {
            "metric_family": "meeting_recording_v2",
            "meetings_scanned": len(meetings),
            "jobs_scanned": len(jobs),
            **metrics,
            **{f"repaired_{key}": value for key, value in repairs.items()},
            "alert": bool(alerts),
            "alert_codes": sorted(alerts),
        },
    )
    if not meetings:
        logger.warn(
            "meetings.operations: no V2 meetings visible to reconciliation query",
            {
                "query_field": F.PROTOCOL_VERSION,
                "alert": True,
            },
        )
    return {"metrics": metrics, "alerts": alerts, "repairs": repairs}


async def _repair(
    meetings: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    *,
    stall_deadline: str,
) -> dict[str, int]:
    """Act on what the snapshot found.

    This pass used to only count. A meeting could strand in ``capturing`` or
    ``synthesizing`` indefinitely, get tallied here every hour, and still render
    to the user as an ordinary spinner with no failure, no notification, and no
    retry. Counting a known-broken row is not observability.
    """
    repairs = {"dispatched_jobs": 0, "stalled_meetings": 0}

    # 1. Redeliver work that committed durably but never reached Cloud Tasks.
    #    The job and outbox row are authoritative; dispatch is only the hint.
    for job in jobs:
        if job.get("state") not in (F.JOB_PENDING, F.JOB_RETRY):
            continue
        if job.get("dispatch_state") == "dispatched":
            continue
        uid = str(job.get("user_id", ""))
        job_id = str(job.get("job_id", ""))
        if not uid or not job_id:
            continue
        try:
            await tasks.dispatch_job(uid, job_id)
            repairs["dispatched_jobs"] += 1
        except Exception as exc:
            logger.error(
                "meetings.operations: reconciliation dispatch failed",
                {
                    "meeting_id": job.get("meeting_id"),
                    "job_id": job_id,
                    "error_code": "reconciliation_dispatch_failed",
                    "error": str(exc),
                },
            )

    # 2. Stamp meetings that have been non-terminal past every legitimate
    #    deadline. FAIL_PROCESSING_TIMEOUT existed for exactly this and had no
    #    writer, which is why a stall was indistinguishable from progress.
    for meeting in meetings:
        if meeting.get(F.STATUS) not in F.ACTIVE_STATUSES:
            continue
        if meeting.get(F.DELETION_STATE):
            continue
        if str(meeting.get(F.UPDATED_AT, "")) > stall_deadline:
            continue
        uid = str(meeting.get("_uid", ""))
        meeting_id = str(meeting.get("meeting_id", ""))
        if not uid or not meeting_id:
            continue
        try:
            transitioned, _status_now = await store.transition_status(
                uid,
                meeting_id,
                from_statuses=F.ACTIVE_STATUSES,
                to_status=F.STATUS_NEEDS_ATTENTION,
                stage=F.STAGE_NEEDS_ATTENTION,
                # The evidence is intact; only the handoff stalled. Leaving this
                # retryable is what makes the user's retry affordance real.
                extra=store.failure_meta(
                    code=F.FAIL_PROCESSING_TIMEOUT, retryable=True
                ),
            )
            if not transitioned:
                continue
            await notifications.notify_settled(uid, meeting_id)
            repairs["stalled_meetings"] += 1
            logger.warn(
                "meetings.operations: stalled meeting marked needs_attention",
                {
                    "meeting_id": meeting_id,
                    "prior_status": meeting.get(F.STATUS),
                    "prior_stage": meeting.get(F.PROCESSING_STAGE),
                    "updated_at": meeting.get(F.UPDATED_AT),
                    "error_code": F.FAIL_PROCESSING_TIMEOUT,
                    "alert": True,
                },
            )
        except Exception as exc:
            logger.error(
                "meetings.operations: stall repair failed",
                {
                    "meeting_id": meeting_id,
                    "error_code": "reconciliation_stall_repair_failed",
                    "error": str(exc),
                },
            )
    return repairs
