"""Bounded Meeting V2 reconciliation metrics and alert hooks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F


async def reconciliation_snapshot(*, limit: int = 200) -> dict[str, Any]:
    now = datetime.now(UTC)
    stale_capture_before = (now - timedelta(minutes=5)).isoformat()

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
    alerts = {key: value for key, value in metrics.items() if value}
    logger.info(
        "meetings.operations: reconciliation snapshot",
        {
            "metric_family": "meeting_recording_v2",
            "meetings_scanned": len(meetings),
            "jobs_scanned": len(jobs),
            **metrics,
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
    return {"metrics": metrics, "alerts": alerts}
