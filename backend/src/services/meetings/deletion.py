"""Exact-generation, retryable Meeting Recording V2 deletion saga."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore as gcloud_firestore

from ..firebase import admin_firestore
from . import fields as F
from . import gcs_audio, refs, store
from .audit import audit_event, txn_create


def _deletion_ref(uid: str, meeting_id: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.DELETIONS_SUBCOLLECTION)
        .document(meeting_id)
    )


async def request_deletion(
    uid: str,
    meeting_id: str,
    *,
    actor_identity: str,
    correlation_id: str,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()

    def _run() -> dict[str, Any]:
        db = admin_firestore()
        meeting_ref = refs.meetings_ref(uid).document(meeting_id)
        deletion_ref = _deletion_ref(uid, meeting_id)
        txn = db.transaction()

        @gcloud_firestore.transactional
        def _execute(transaction) -> dict[str, Any]:
            meeting_snap = meeting_ref.get(transaction=transaction)
            deletion_snap = deletion_ref.get(transaction=transaction)
            if not meeting_snap.exists:
                return {"state": "missing"}
            meeting = meeting_snap.to_dict() or {}
            existing = deletion_snap.to_dict() or {}
            if existing.get("state") == F.STAGE_DELETE_COMPLETE:
                return existing
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            deletion_id = str(existing.get("deletion_id") or uuid.uuid4().hex)
            saga = {
                **existing,
                "deletion_id": deletion_id,
                "user_id": uid,
                "meeting_id": meeting_id,
                F.CAPTURE_RUN_ID: meeting.get(F.CAPTURE_RUN_ID, ""),
                F.CAPTURE_FENCE: meeting.get(F.CAPTURE_FENCE, 0),
                "state": F.STAGE_BLOCK_NEW_WORK,
                "local_coordination_state": "server_block_committed",
                "receipts": existing.get("receipts") or {},
                "requested_at": existing.get("requested_at") or now,
                "updated_at": now,
                "correlation_id": correlation_id,
            }
            if deletion_snap.exists:
                transaction.update(deletion_ref, saga)
            else:
                txn_create(transaction, deletion_ref, saga)
            transaction.update(
                meeting_ref,
                {
                    F.STATUS: F.STATUS_DELETE_REQUESTED,
                    F.PROCESSING_STAGE: F.STAGE_BLOCK_NEW_WORK,
                    F.DELETION_STATE: F.STAGE_BLOCK_NEW_WORK,
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now,
                    F.STATUS_REVISION: gcloud_firestore.Increment(1),
                },
            )
            audit_event(
                transaction,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type="delete_requested",
                occurred_at=now,
                actor_type="user",
                actor_identity=actor_identity,
                capture_run_id=str(meeting.get(F.CAPTURE_RUN_ID, "")),
                capture_fence=int(meeting.get(F.CAPTURE_FENCE, 0)),
                prior_state=str(meeting.get(F.STATUS, "")),
                next_state=F.STATUS_DELETE_REQUESTED,
                reason_code="user_requested_deletion",
                correlation_id=correlation_id,
            )
            return saga

        return _execute(txn)

    return await asyncio.to_thread(_run)


async def _targets(uid: str, meeting_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meeting = await store.get_meeting(uid, meeting_id)
    if meeting is None:
        return {}, []
    run_id = str(meeting.get(F.CAPTURE_RUN_ID, ""))
    count = int(meeting.get(F.SEGMENT_COUNT, 0))

    def _read() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for seq in range(count):
            snap = refs.segments_ref(uid, meeting_id, run_id).document(f"{seq:06d}").get()
            if snap.exists:
                receipt = (snap.to_dict() or {}).get("upload_receipt") or {}
                if receipt.get("object") and receipt.get("generation"):
                    rows.append(
                        {
                            "kind": "audio",
                            "path": receipt["object"],
                            "generation": str(receipt["generation"]),
                        }
                    )
        artifacts = meeting.get(F.ARTIFACTS) or {}
        for name, pointer in artifacts.items():
            if name == "provider_attempts" and isinstance(pointer, dict):
                for item in pointer.get("items") or []:
                    if item.get("path") and item.get("generation"):
                        rows.append(
                            {
                                "kind": "transcript",
                                "path": item["path"],
                                "generation": str(item["generation"]),
                            }
                        )
            elif isinstance(pointer, dict) and pointer.get("path") and pointer.get("generation"):
                rows.append(
                    {
                        "kind": "transcript",
                        "path": pointer["path"],
                        "generation": str(pointer["generation"]),
                    }
                )
        manifest = str(meeting.get(F.MANIFEST_SHA256, ""))
        if manifest and run_id:
            job_id = F.job_id_for(uid, meeting_id, run_id, manifest)
            job_snap = refs.jobs_ref(uid).document(job_id).get()
            job = job_snap.to_dict() or {}
            for attempt in (job.get("segment_attempts") or {}).values():
                pointer = attempt.get("artifact") if isinstance(attempt, dict) else None
                if isinstance(pointer, dict) and pointer.get("path") and pointer.get("generation"):
                    rows.append(
                        {
                            "kind": "transcript",
                            "path": pointer["path"],
                            "generation": str(pointer["generation"]),
                        }
                    )
                for history in attempt.get("history", []) if isinstance(attempt, dict) else []:
                    historical = history.get("artifact") if isinstance(history, dict) else None
                    if (
                        isinstance(historical, dict)
                        and historical.get("path")
                        and historical.get("generation")
                    ):
                        rows.append(
                            {
                                "kind": "transcript",
                                "path": historical["path"],
                                "generation": str(historical["generation"]),
                            }
                        )
        deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            deduped[(row["kind"], row["path"], row["generation"])] = row
        rows = list(deduped.values())
        return rows

    return meeting, await asyncio.to_thread(_read)


async def _record_receipt(
    uid: str,
    meeting_id: str,
    *,
    target: dict[str, Any],
    receipt: dict[str, Any],
    correlation_id: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    key = hashlib.sha256(
        f"{target['kind']}:{target['path']}:{target['generation']}".encode()
    ).hexdigest()

    def _run() -> None:
        db = admin_firestore()
        meeting_ref = refs.meetings_ref(uid).document(meeting_id)
        deletion_ref = _deletion_ref(uid, meeting_id)
        txn = db.transaction()

        @gcloud_firestore.transactional
        def _execute(transaction) -> None:
            meeting_snap = meeting_ref.get(transaction=transaction)
            deletion_snap = deletion_ref.get(transaction=transaction)
            meeting = meeting_snap.to_dict() or {}
            deletion = deletion_snap.to_dict() or {}
            receipts = dict(deletion.get("receipts") or {})
            if key in receipts:
                return
            receipts[key] = {
                **receipt,
                "kind": target["kind"],
                "receipt_id": uuid.uuid4().hex,
                "recorded_at": now,
            }
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            stage = (
                F.STAGE_CLOUD_AUDIO_DELETE
                if target["kind"] == "audio"
                else F.STAGE_TRANSCRIPT_DELETE
            )
            transaction.update(
                deletion_ref,
                {
                    "state": stage,
                    "receipts": receipts,
                    "updated_at": now,
                },
            )
            transaction.update(
                meeting_ref,
                {
                    F.DELETION_STATE: stage,
                    F.PROCESSING_STAGE: stage,
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now,
                },
            )
            audit_event(
                transaction,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type=stage,
                occurred_at=now,
                capture_run_id=str(meeting.get(F.CAPTURE_RUN_ID, "")),
                capture_fence=int(meeting.get(F.CAPTURE_FENCE, 0)),
                prior_state=str(deletion.get("state", "")),
                next_state=stage,
                artifacts=[
                    {
                        "path": target["path"],
                        "generation": target["generation"],
                        "outcome": receipt["outcome"],
                    }
                ],
                reason_code=receipt["outcome"],
                correlation_id=correlation_id,
            )

        _execute(txn)

    await asyncio.to_thread(_run)


async def run_deletion(uid: str, meeting_id: str) -> dict[str, Any]:
    meeting, targets = await _targets(uid, meeting_id)
    if not meeting:
        return {"state": "missing"}
    deletion_snap = await asyncio.to_thread(_deletion_ref(uid, meeting_id).get)
    deletion = deletion_snap.to_dict() or {}
    correlation_id = str(deletion.get("correlation_id", ""))
    receipts = deletion.get("receipts") or {}
    for target in targets:
        key = hashlib.sha256(
            f"{target['kind']}:{target['path']}:{target['generation']}".encode()
        ).hexdigest()
        if key in receipts:
            continue
        receipt = await gcs_audio.delete_exact_object(
            target["path"],
            target["generation"],
            transcript=target["kind"] == "transcript",
        )
        await _record_receipt(
            uid,
            meeting_id,
            target=target,
            receipt=receipt,
            correlation_id=correlation_id,
        )

    now = datetime.now(UTC).isoformat()

    def _tombstone() -> dict[str, Any]:
        db = admin_firestore()
        meeting_ref = refs.meetings_ref(uid).document(meeting_id)
        capture_run_id = str(meeting.get(F.CAPTURE_RUN_ID, ""))
        run_ref = (
            refs.capture_run_ref(uid, meeting_id, capture_run_id) if capture_run_id else None
        )
        deletion_ref = _deletion_ref(uid, meeting_id)
        txn = db.transaction()

        @gcloud_firestore.transactional
        def _execute(transaction) -> dict[str, Any]:
            meeting_snap = meeting_ref.get(transaction=transaction)
            deletion_snap = deletion_ref.get(transaction=transaction)
            current = meeting_snap.to_dict() or {}
            saga = deletion_snap.to_dict() or {}
            if saga.get("state") == F.STAGE_DELETE_COMPLETE:
                return saga
            sequence = int(current.get(F.AUDIT_SEQUENCE, 0)) + 1
            if run_ref is not None:
                transaction.update(
                    run_ref,
                    {
                        F.CAPTURE_RUN_STATE: F.CAPTURE_RUN_DELETED,
                        F.UPDATED_AT: now,
                    },
                )
            transaction.update(
                meeting_ref,
                {
                    F.STATUS: F.STATUS_DELETE_COMPLETE,
                    F.PROCESSING_STAGE: F.STAGE_DELETE_COMPLETE,
                    F.DELETION_STATE: F.STAGE_DELETE_COMPLETE,
                    F.DELETED_AT: now,
                    F.NOTE: gcloud_firestore.DELETE_FIELD,
                    F.TITLE: gcloud_firestore.DELETE_FIELD,
                    F.START_TIME: gcloud_firestore.DELETE_FIELD,
                    F.END_TIME: gcloud_firestore.DELETE_FIELD,
                    F.ARTIFACTS: gcloud_firestore.DELETE_FIELD,
                    F.AUDIT_SEQUENCE: sequence,
                    F.STATUS_REVISION: gcloud_firestore.Increment(1),
                    F.UPDATED_AT: now,
                },
            )
            transaction.update(
                deletion_ref,
                {
                    "state": F.STAGE_DELETE_COMPLETE,
                    "completed_at": now,
                    "updated_at": now,
                },
            )
            audit_event(
                transaction,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type="delete_complete",
                occurred_at=now,
                capture_run_id=str(current.get(F.CAPTURE_RUN_ID, "")),
                capture_fence=int(current.get(F.CAPTURE_FENCE, 0)),
                prior_state=str(current.get(F.DELETION_STATE, "")),
                next_state=F.STAGE_DELETE_COMPLETE,
                reason_code="exact_generation_deletion_complete",
                correlation_id=correlation_id,
            )
            return {**saga, "state": F.STAGE_DELETE_COMPLETE, "completed_at": now}

        return _execute(txn)

    return await asyncio.to_thread(_tombstone)
