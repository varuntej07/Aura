"""Meeting-notes Firestore store - claim, status transitions, note persistence.

Claim is the money path and mirrors entitlement.py's transactional counter
idiom: one Firestore transaction reads the event's claim lock plus the monthly
counter, then either returns the existing claim (same device rejoining), denies
(cap or cross-device conflict), or creates the meeting doc, sets the lock, and
charges the counter atomically. The counter is charged HERE and never by the
synthesis worker, so Cloud Tasks retries can never double-bill.

Unlike the chat/web-surf daily counters (which fail open because they meter a
cheap resource), claim FAILS CLOSED: a Firestore outage raises and the handler
answers 503, because every allowed claim commits real STT+LLM spend.

All Firestore work runs in ``asyncio.to_thread`` so the event loop stays
unblocked, matching every other store in this backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore as gcloud_firestore

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import evidence
from . import fields as F


def _meetings_ref(uid: str):
    return (
        admin_firestore().collection(F.PARENT_COLLECTION).document(uid).collection(F.SUBCOLLECTION)
    )


def _claim_ref(uid: str, event_key: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.CLAIMS_SUBCOLLECTION)
        .document(event_key)
    )


def _usage_ref(uid: str, month_key: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.USAGE_SUBCOLLECTION)
        .document(f"meetings_{month_key}")
    )


def _capture_run_ref(uid: str, meeting_id: str, capture_run_id: str):
    return (
        _meetings_ref(uid)
        .document(meeting_id)
        .collection(F.CAPTURE_RUNS_SUBCOLLECTION)
        .document(capture_run_id)
    )


def _segments_ref(uid: str, meeting_id: str, capture_run_id: str):
    return _capture_run_ref(uid, meeting_id, capture_run_id).collection(F.SEGMENTS_SUBCOLLECTION)


def _jobs_ref(uid: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.JOBS_SUBCOLLECTION)
    )


def _outbox_ref(uid: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.JOB_OUTBOX_SUBCOLLECTION)
    )


def _audit_ref(uid: str, meeting_id: str):
    return _meetings_ref(uid).document(meeting_id).collection(F.AUDIT_SUBCOLLECTION)


class MeetingIntegrityError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StaleCaptureFenceError(MeetingIntegrityError):
    def __init__(self):
        super().__init__(F.FAIL_STALE_CAPTURE_FENCE, "Capture fence is stale.")


class DeletedMeetingError(MeetingIntegrityError):
    def __init__(self):
        super().__init__(F.FAIL_DELETION_IN_PROGRESS, "Meeting deletion is in progress.")


def _actor_hash(actor_identity: str) -> str:
    return hashlib.sha256(actor_identity.encode("utf-8")).hexdigest()


def _txn_create(txn: Any, ref: Any, value: dict[str, Any]) -> None:
    """Use Firestore's create-only primitive; old in-repo fakes fall back to set."""
    create = getattr(txn, "create", None)
    if callable(create):
        create(ref, value)
    else:
        txn.set(ref, value)


def _audit_event(
    txn: Any,
    *,
    uid: str,
    meeting_id: str,
    sequence: int,
    event_type: str,
    occurred_at: str,
    actor_type: str = "server",
    actor_identity: str = "juno-backend",
    runtime_instance_id: str = "",
    capture_run_id: str = "",
    capture_fence: int = 0,
    job_id: str = "",
    attempt: int = 0,
    lease_token: str = "",
    prior_state: str = "",
    next_state: str = "",
    artifacts: list[dict[str, Any]] | None = None,
    reason_code: str = "",
    correlation_id: str = "",
    causation_id: str = "",
    policy_version: str = "",
) -> str:
    event_id = uuid.uuid4().hex
    envelope = {
        "event_id": event_id,
        "sequence": sequence,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "recorded_at": datetime.now(UTC).isoformat(),
        "actor_type": actor_type,
        "actor_identity_hash": _actor_hash(actor_identity),
        F.RUNTIME_INSTANCE_ID: runtime_instance_id,
        "meeting_id": meeting_id,
        F.CAPTURE_RUN_ID: capture_run_id,
        F.CAPTURE_FENCE: capture_fence,
        "job_id": job_id,
        "attempt": attempt,
        "lease_token_hash": _actor_hash(lease_token) if lease_token else "",
        "prior_state": prior_state,
        "next_state": next_state,
        "artifacts": artifacts or [],
        "reason_code": reason_code,
        "software_version": F.SOFTWARE_COMPONENT,
        "schema_version": F.AUDIT_SCHEMA_VERSION,
        "policy_version": policy_version,
        "correlation_id": correlation_id or event_id,
        "causation_id": causation_id,
    }
    _txn_create(txn, _audit_ref(uid, meeting_id).document(event_id), envelope)
    return event_id


def event_key_for(event_id: str) -> str:
    """Deterministic, Firestore-safe doc id for an event's claim lock.
    Calendar instance ids and manual ids can carry characters we'd rather not
    trust in a doc path, so the key is always the sha1 hex of the raw id."""
    return hashlib.sha1(event_id.encode("utf-8")).hexdigest()


def _seconds_until_next_month(now: datetime) -> int:
    if now.month == 12:
        reset = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        reset = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    return max(0, int((reset - now).total_seconds()))


@dataclass
class ClaimResult:
    meeting_id: str = ""
    cap_minutes: int = 0
    denied_cap: bool = False
    denied_conflict: bool = False
    seconds_until_reset: int = 0
    rejoined: bool = False
    capture_run_id: str = ""
    capture_fence: int = 0
    lease_expires_at: str = ""


async def claim_meeting(
    uid: str,
    *,
    event_id: str,
    title: str,
    start_time: str,
    end_time: str,
    device_id: str,
    effective_tier: str,
    installation_id: str | None = None,
    runtime_instance_id: str = "",
    correlation_id: str = "",
) -> ClaimResult:
    """Atomically claim one meeting capture slot. Raises on Firestore failure
    (fails closed; the handler answers 503 and the client backs off).

    The claim lock self-expires at the event's end plus CLAIM_GRACE_MINUTES:
    a drop-and-rejoin inside that window returns the same meeting_id with no
    second charge, while a fresh capture of the same event much later gets a
    new meeting and a new charge."""
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    month_key = now.strftime("%Y%m")
    event_key = event_key_for(event_id)
    is_capped_tier = effective_tier != "pro"
    cap_minutes = F.FREE_SYNTHESIS_CAP_MINUTES if is_capped_tier else F.PRO_SYNTHESIS_CAP_MINUTES
    installation_id = installation_id or device_id
    lease_expires_at = now + timedelta(minutes=F.CAPTURE_LEASE_MINUTES)

    # The lock expires at the event's scheduled end plus grace, or (for manual
    # captures and unparseable times) a full capture-length window from now.
    try:
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        expires_at_ms = int(end_dt.timestamp() * 1000) + F.CLAIM_GRACE_MINUTES * 60_000
    except (ValueError, AttributeError):
        expires_at_ms = now_ms + (F.MAX_CAPTURE_MINUTES + F.CLAIM_GRACE_MINUTES) * 60_000
    expires_at_ms = max(expires_at_ms, now_ms + F.CLAIM_GRACE_MINUTES * 60_000)

    def _run() -> ClaimResult:
        db = admin_firestore()
        lock_ref = _claim_ref(uid, event_key)
        usage_ref = _usage_ref(uid, month_key)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> ClaimResult:
            lock_snap = lock_ref.get(transaction=txn)
            usage_snap = usage_ref.get(transaction=txn)

            lock = lock_snap.to_dict() or {}
            if lock and lock.get(F.CLAIM_EXPIRES_AT_MS, 0) > now_ms:
                lock_owner = lock.get(F.CLAIM_INSTALLATION_ID) or lock.get(F.CLAIM_DEVICE_ID)
                if lock_owner != installation_id:
                    return ClaimResult(denied_conflict=True)
                # Same-device rejoin is only a continuation while the meeting
                # can still accept audio. Once /complete moved it past
                # "capturing" (synthesis may already be running), reusing the
                # id would record into a meeting whose uploads 409 forever -
                # fall through and mint a fresh meeting instead.
                locked_meeting_id = lock.get(F.CLAIM_MEETING_ID, "")
                meeting_snap = (
                    _meetings_ref(uid)
                    .document(locked_meeting_id)
                    .get(
                        transaction=txn,
                    )
                )
                meeting_status = (meeting_snap.to_dict() or {}).get(F.STATUS, "")
                if meeting_status == F.STATUS_CAPTURING:
                    meeting = meeting_snap.to_dict() or {}
                    capture_run_id = str(
                        lock.get(F.CLAIM_CAPTURE_RUN_ID)
                        or meeting.get(F.CAPTURE_RUN_ID)
                        or uuid.uuid4().hex
                    )
                    # The fence exists to lock out a SECOND writer, so only a
                    # genuinely different runtime advances it. The same runtime
                    # re-claiming its own live run is a resume, and bumping the
                    # fence there invalidated audio it had already recorded:
                    # the desktop stamps segments at capture time and cannot
                    # restamp them, so every later upload 409'd forever.
                    current_fence = max(
                        int(lock.get(F.CLAIM_CAPTURE_FENCE, 0)),
                        int(meeting.get(F.CAPTURE_FENCE, 0)),
                    )
                    prior_runtime = str(
                        lock.get(F.CLAIM_RUNTIME_INSTANCE_ID)
                        or meeting.get(F.RUNTIME_INSTANCE_ID)
                        or ""
                    )
                    resumed = (
                        bool(runtime_instance_id)
                        and prior_runtime == runtime_instance_id
                        and current_fence >= 1
                    )
                    next_fence = current_fence if resumed else current_fence + 1
                    sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
                    txn.update(
                        _meetings_ref(uid).document(locked_meeting_id),
                        {
                            F.CAPTURE_FENCE: next_fence,
                            F.RUNTIME_INSTANCE_ID: runtime_instance_id,
                            F.LEASE_EXPIRES_AT: lease_expires_at.isoformat(),
                            F.UPDATED_AT: now.isoformat(),
                            F.AUDIT_SEQUENCE: sequence,
                        },
                    )
                    txn.set(
                        _capture_run_ref(
                            uid,
                            locked_meeting_id,
                            capture_run_id,
                        ),
                        {
                            F.CAPTURE_RUN_ID: capture_run_id,
                            F.CAPTURE_FENCE: next_fence,
                            F.INSTALLATION_ID: installation_id,
                            F.RUNTIME_INSTANCE_ID: runtime_instance_id,
                            F.LEASE_EXPIRES_AT: lease_expires_at.isoformat(),
                            F.CAPTURE_RUN_STATE: F.CAPTURE_RUN_CAPTURING,
                            F.UPDATED_AT: now.isoformat(),
                        },
                    )
                    txn.update(
                        lock_ref,
                        {
                            F.CLAIM_CAPTURE_FENCE: next_fence,
                            F.CLAIM_RUNTIME_INSTANCE_ID: runtime_instance_id,
                            F.CLAIM_LEASE_EXPIRES_AT: lease_expires_at.isoformat(),
                        },
                    )
                    _audit_event(
                        txn,
                        uid=uid,
                        meeting_id=locked_meeting_id,
                        sequence=sequence,
                        event_type="capture_claimed",
                        occurred_at=now.isoformat(),
                        actor_type="installation",
                        actor_identity=installation_id,
                        runtime_instance_id=runtime_instance_id,
                        capture_run_id=capture_run_id,
                        capture_fence=next_fence,
                        prior_state=F.STATUS_CAPTURING,
                        next_state=F.STATUS_CAPTURING,
                        reason_code="capture_resumed" if resumed else "capture_recovered",
                        correlation_id=correlation_id,
                    )
                    return ClaimResult(
                        meeting_id=locked_meeting_id,
                        cap_minutes=int(lock.get(F.CAP_MINUTES, cap_minutes)),
                        rejoined=True,
                        capture_run_id=capture_run_id,
                        capture_fence=next_fence,
                        lease_expires_at=lease_expires_at.isoformat(),
                    )

            count = int((usage_snap.to_dict() or {}).get("count", 0))
            if is_capped_tier and count >= F.MONTHLY_MEETING_CAP:
                return ClaimResult(
                    denied_cap=True,
                    seconds_until_reset=_seconds_until_next_month(now),
                )

            meeting_id = uuid.uuid4().hex
            capture_run_id = uuid.uuid4().hex
            capture_fence = 1
            txn.set(
                _meetings_ref(uid).document(meeting_id),
                {
                    F.EVENT_ID: event_id,
                    F.TITLE: title,
                    F.START_TIME: start_time,
                    F.END_TIME: end_time,
                    F.DEVICE_ID: device_id,
                    F.INSTALLATION_ID: installation_id,
                    F.RUNTIME_INSTANCE_ID: runtime_instance_id,
                    F.PROTOCOL_VERSION: F.MEETING_SCHEMA_VERSION,
                    F.CAPTURE_RUN_ID: capture_run_id,
                    F.CAPTURE_FENCE: capture_fence,
                    F.LEASE_EXPIRES_AT: lease_expires_at.isoformat(),
                    F.STATUS: F.STATUS_CAPTURING,
                    F.CAP_MINUTES: cap_minutes,
                    F.SEGMENTS: [],
                    F.CREATED_AT: now.isoformat(),
                    F.UPDATED_AT: now.isoformat(),
                    F.PROCESSING_STAGE: F.STAGE_CAPTURING,
                    F.STATUS_REVISION: 0,
                    F.ATTEMPT_COUNT: 0,
                    F.AUDIT_SEQUENCE: 1,
                    F.ARTIFACT_REVISION: 0,
                },
            )
            txn.set(
                _capture_run_ref(uid, meeting_id, capture_run_id),
                {
                    F.CAPTURE_RUN_ID: capture_run_id,
                    "meeting_id": meeting_id,
                    F.CAPTURE_FENCE: capture_fence,
                    F.INSTALLATION_ID: installation_id,
                    F.RUNTIME_INSTANCE_ID: runtime_instance_id,
                    F.LEASE_EXPIRES_AT: lease_expires_at.isoformat(),
                    F.CAPTURE_RUN_STATE: F.CAPTURE_RUN_CAPTURING,
                    F.CREATED_AT: now.isoformat(),
                    F.UPDATED_AT: now.isoformat(),
                },
            )
            txn.set(
                lock_ref,
                {
                    F.CLAIM_EVENT_ID: event_id,
                    F.CLAIM_MEETING_ID: meeting_id,
                    F.CLAIM_DEVICE_ID: device_id,
                    F.CLAIM_INSTALLATION_ID: installation_id,
                    F.CLAIM_RUNTIME_INSTANCE_ID: runtime_instance_id,
                    F.CLAIM_CAPTURE_RUN_ID: capture_run_id,
                    F.CLAIM_CAPTURE_FENCE: capture_fence,
                    F.CLAIM_LEASE_EXPIRES_AT: lease_expires_at.isoformat(),
                    F.CLAIM_EXPIRES_AT_MS: expires_at_ms,
                    F.CAP_MINUTES: cap_minutes,
                },
            )
            txn.set(usage_ref, {"count": count + 1})
            _audit_event(
                txn,
                uid=uid,
                meeting_id=meeting_id,
                sequence=1,
                event_type="capture_claimed",
                occurred_at=now.isoformat(),
                actor_type="installation",
                actor_identity=installation_id,
                runtime_instance_id=runtime_instance_id,
                capture_run_id=capture_run_id,
                capture_fence=capture_fence,
                prior_state="capture_requested",
                next_state=F.STATUS_CAPTURING,
                reason_code="capture_granted",
                correlation_id=correlation_id,
            )
            return ClaimResult(
                meeting_id=meeting_id,
                cap_minutes=cap_minutes,
                capture_run_id=capture_run_id,
                capture_fence=capture_fence,
                lease_expires_at=lease_expires_at.isoformat(),
            )

        return _execute(transaction)

    result = await asyncio.to_thread(_run)
    logger.info(
        "meetings.store: claim",
        {
            "user_id": uid,
            "event_key": event_key,
            "meeting_id": result.meeting_id,
            "denied_cap": result.denied_cap,
            "denied_conflict": result.denied_conflict,
            "rejoined": result.rejoined,
            "tier": effective_tier,
        },
    )
    return result


async def get_meeting(uid: str, meeting_id: str) -> dict[str, Any] | None:
    """One meeting doc, or None when missing. Raises on Firestore failure so
    ownership checks in the handlers never silently pass on an outage."""

    def _read() -> dict[str, Any] | None:
        snap = _meetings_ref(uid).document(meeting_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data["meeting_id"] = snap.id
        return data

    return await asyncio.to_thread(_read)


async def append_segment_meta(
    uid: str,
    meeting_id: str,
    *,
    seq: int,
    start_ms: int,
    duration_ms: int,
    incomplete: bool,
) -> None:
    """Record one uploaded segment's offsets on the meeting doc. ArrayUnion
    makes a client upload retry idempotent (an identical element is a no-op).
    `incomplete` marks a segment that may contain a silent hole (device
    re-bind mid-segment) so synthesis can caveat the note honestly."""

    def _update() -> None:
        _meetings_ref(uid).document(meeting_id).update(
            {
                F.SEGMENTS: gcloud_firestore.ArrayUnion(
                    [
                        {
                            "seq": seq,
                            "start_ms": start_ms,
                            "duration_ms": duration_ms,
                            "incomplete": incomplete,
                        },
                    ]
                ),
                F.UPDATED_AT: datetime.now(UTC).isoformat(),
                F.PROCESSING_STAGE: F.STAGE_UPLOADING,
                F.RETRYABLE: False,
                F.FAILURE_CODE: gcloud_firestore.DELETE_FIELD,
                F.FAILURE_MESSAGE: gcloud_firestore.DELETE_FIELD,
            }
        )

    await asyncio.to_thread(_update)


@dataclass(frozen=True)
class UploadReceipt:
    receipt_id: str
    object: str
    generation: str
    content_sha256: str
    byte_length: int
    accepted_at: str
    crc32c: str | None = None
    reconciled: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "object": self.object,
            "generation": self.generation,
            "content_sha256": self.content_sha256,
            "byte_length": self.byte_length,
            "accepted_at": self.accepted_at,
        }


@dataclass(frozen=True)
class CompletionResult:
    receipt: dict[str, Any] | None = None
    conflict_code: str = ""
    job_id: str = ""
    idempotent: bool = False


def _receipt_from_doc(data: dict[str, Any]) -> UploadReceipt:
    receipt = data.get("upload_receipt") or {}
    return UploadReceipt(
        receipt_id=str(receipt["receipt_id"]),
        object=str(receipt["object"]),
        generation=str(receipt["generation"]),
        content_sha256=str(receipt["content_sha256"]),
        byte_length=int(receipt["byte_length"]),
        accepted_at=str(receipt["accepted_at"]),
        crc32c=receipt.get("crc32c"),
        reconciled=bool(receipt.get("reconciled", False)),
    )


async def persist_v2_segment(
    uid: str,
    meeting_id: str,
    capture_run_id: str,
    *,
    capture_fence: int,
    segment: dict[str, Any],
    immutable_object: Any,
    idempotency_key: str,
    runtime_instance_id: str,
    correlation_id: str,
) -> UploadReceipt:
    """Commit one deterministic segment identity and its real GCS receipt."""
    now = datetime.now(UTC).isoformat()
    seq = int(segment["seq"])

    def _run() -> tuple[UploadReceipt | None, str]:
        db = admin_firestore()
        meeting_ref = _meetings_ref(uid).document(meeting_id)
        run_ref = _capture_run_ref(uid, meeting_id, capture_run_id)
        segment_ref = _segments_ref(uid, meeting_id, capture_run_id).document(f"{seq:06d}")
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> tuple[UploadReceipt | None, str]:
            meeting_snap = meeting_ref.get(transaction=txn)
            run_snap = run_ref.get(transaction=txn)
            existing_snap = segment_ref.get(transaction=txn)
            if not meeting_snap.exists or not run_snap.exists:
                return None, "unknown_capture_run"
            meeting = meeting_snap.to_dict() or {}
            run = run_snap.to_dict() or {}
            if meeting.get(F.DELETION_STATE) or meeting.get(F.STATUS) in (
                F.STATUS_DELETE_REQUESTED,
                F.STATUS_DELETE_COMPLETE,
            ):
                return None, F.FAIL_DELETION_IN_PROGRESS
            if (
                int(meeting.get(F.CAPTURE_FENCE, -1)) != capture_fence
                or int(run.get(F.CAPTURE_FENCE, -1)) != capture_fence
            ):
                return None, F.FAIL_STALE_CAPTURE_FENCE
            if run.get(F.CAPTURE_RUN_STATE) != F.CAPTURE_RUN_CAPTURING:
                return None, "capture_run_not_accepting_uploads"

            identity = {field: segment[field] for field in evidence.SEGMENT_IDENTITY_FIELDS}
            identity.update(
                {
                    "meeting_id": meeting_id,
                    F.CAPTURE_RUN_ID: capture_run_id,
                    F.CAPTURE_FENCE: capture_fence,
                }
            )
            if existing_snap.exists:
                existing = existing_snap.to_dict() or {}
                if any(existing.get(key) != value for key, value in identity.items()):
                    sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
                    txn.update(
                        meeting_ref,
                        {
                            F.FAILURE_CODE: F.FAIL_SEGMENT_IDENTITY_CONFLICT,
                            F.RETRYABLE: False,
                            F.AUDIT_SEQUENCE: sequence,
                            F.UPDATED_AT: now,
                        },
                    )
                    txn.update(
                        run_ref,
                        {
                            F.CAPTURE_RUN_STATE: F.CAPTURE_RUN_SPLIT_BRAIN,
                            F.FAILURE_CODE: F.FAIL_SEGMENT_IDENTITY_CONFLICT,
                            F.UPDATED_AT: now,
                        },
                    )
                    _audit_event(
                        txn,
                        uid=uid,
                        meeting_id=meeting_id,
                        sequence=sequence,
                        event_type="upload_conflicted",
                        occurred_at=now,
                        actor_type="runtime",
                        actor_identity=runtime_instance_id or "unknown-runtime",
                        runtime_instance_id=runtime_instance_id,
                        capture_run_id=capture_run_id,
                        capture_fence=capture_fence,
                        prior_state=run.get(F.CAPTURE_RUN_STATE, ""),
                        next_state=F.CAPTURE_RUN_SPLIT_BRAIN,
                        artifacts=[
                            {
                                "seq": seq,
                                "persisted_digest": existing.get("content_sha256", ""),
                                "incoming_digest": segment["content_sha256"],
                                "incoming_object": immutable_object.path,
                                "incoming_generation": immutable_object.generation,
                            }
                        ],
                        reason_code=F.FAIL_SEGMENT_IDENTITY_CONFLICT,
                        correlation_id=correlation_id,
                    )
                    return None, F.FAIL_SEGMENT_IDENTITY_CONFLICT
                return _receipt_from_doc(existing), ""

            receipt = UploadReceipt(
                receipt_id=uuid.uuid4().hex,
                object=immutable_object.path,
                generation=immutable_object.generation,
                content_sha256=segment["content_sha256"],
                byte_length=segment["byte_length"],
                accepted_at=now,
                crc32c=immutable_object.crc32c,
                reconciled=immutable_object.reconciled,
            )
            stored_receipt = {
                **receipt.public_dict(),
                "crc32c": receipt.crc32c,
                "etag": immutable_object.etag,
                "content_type": immutable_object.content_type,
                "reconciled": receipt.reconciled,
            }
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            _txn_create(
                txn,
                segment_ref,
                {
                    **identity,
                    "audio_metrics": segment.get("audio_metrics"),
                    "decoded_duration_ms": segment["decoded_duration_ms"],
                    "upload_receipt": stored_receipt,
                    "idempotency_key_hash": _actor_hash(idempotency_key),
                    F.RUNTIME_INSTANCE_ID: runtime_instance_id,
                    F.CREATED_AT: now,
                    F.UPDATED_AT: now,
                    "integrity_status": "verified",
                },
            )
            txn.update(
                meeting_ref,
                {
                    F.PROCESSING_STAGE: F.STAGE_UPLOADING,
                    F.UPDATED_AT: now,
                    F.AUDIT_SEQUENCE: sequence,
                    F.RETRYABLE: False,
                    F.FAILURE_CODE: gcloud_firestore.DELETE_FIELD,
                    F.FAILURE_MESSAGE: gcloud_firestore.DELETE_FIELD,
                },
            )
            _audit_event(
                txn,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type=(
                    "upload_reconciled" if immutable_object.reconciled else "upload_accepted"
                ),
                occurred_at=now,
                actor_type="runtime",
                actor_identity=runtime_instance_id or "unknown-runtime",
                runtime_instance_id=runtime_instance_id,
                capture_run_id=capture_run_id,
                capture_fence=capture_fence,
                prior_state=F.CAPTURE_RUN_CAPTURING,
                next_state=F.CAPTURE_RUN_CAPTURING,
                artifacts=[
                    {
                        "path": receipt.object,
                        "generation": receipt.generation,
                        "digest": receipt.content_sha256,
                        "size": receipt.byte_length,
                        "seq": seq,
                    }
                ],
                reason_code="idempotent_reconciliation" if receipt.reconciled else "",
                correlation_id=correlation_id,
            )
            return receipt, ""

        return _execute(transaction)

    receipt, error = await asyncio.to_thread(_run)
    if error == F.FAIL_STALE_CAPTURE_FENCE:
        raise StaleCaptureFenceError
    if error == F.FAIL_DELETION_IN_PROGRESS:
        raise DeletedMeetingError
    if error:
        raise MeetingIntegrityError(error, error.replace("_", " "))
    if receipt is None:
        raise MeetingIntegrityError("upload_receipt_missing", "Upload receipt was not committed.")
    return receipt


async def record_upload_conflict(
    uid: str,
    meeting_id: str,
    capture_run_id: str,
    *,
    capture_fence: int,
    seq: int,
    incoming_digest: str,
    object_path: str,
    runtime_instance_id: str,
    correlation_id: str,
) -> None:
    now = datetime.now(UTC).isoformat()

    def _run() -> None:
        db = admin_firestore()
        meeting_ref = _meetings_ref(uid).document(meeting_id)
        run_ref = _capture_run_ref(uid, meeting_id, capture_run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> None:
            meeting_snap = meeting_ref.get(transaction=txn)
            run_snap = run_ref.get(transaction=txn)
            meeting = meeting_snap.to_dict() or {}
            run = run_snap.to_dict() or {}
            if not meeting or not run:
                return
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            txn.update(
                meeting_ref,
                {
                    F.FAILURE_CODE: F.FAIL_IMMUTABLE_OBJECT_CONFLICT,
                    F.RETRYABLE: False,
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now,
                },
            )
            txn.update(
                run_ref,
                {
                    F.CAPTURE_RUN_STATE: F.CAPTURE_RUN_SPLIT_BRAIN,
                    F.FAILURE_CODE: F.FAIL_IMMUTABLE_OBJECT_CONFLICT,
                    F.UPDATED_AT: now,
                },
            )
            _audit_event(
                txn,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type="upload_conflicted",
                occurred_at=now,
                actor_type="runtime",
                actor_identity=runtime_instance_id or "unknown-runtime",
                runtime_instance_id=runtime_instance_id,
                capture_run_id=capture_run_id,
                capture_fence=capture_fence,
                prior_state=str(run.get(F.CAPTURE_RUN_STATE, "")),
                next_state=F.CAPTURE_RUN_SPLIT_BRAIN,
                artifacts=[
                    {
                        "path": object_path,
                        "incoming_digest": incoming_digest,
                        "seq": seq,
                    }
                ],
                reason_code=F.FAIL_IMMUTABLE_OBJECT_CONFLICT,
                correlation_id=correlation_id,
            )

        _execute(transaction)

    await asyncio.to_thread(_run)


async def verify_v2_completion(
    uid: str,
    meeting_id: str,
    capture_run_id: str,
    *,
    capture_fence: int,
    segments: list[dict[str, Any]],
    segment_digests: list[str],
    segment_count: int,
    total_duration_ms: int,
    reason: str,
    manifest_sha256: str,
    idempotency_key: str,
    runtime_instance_id: str,
    correlation_id: str,
) -> CompletionResult:
    """Verify receipts and atomically create uploaded state, job, outbox, and audit."""
    now = datetime.now(UTC).isoformat()
    computed_manifest = evidence.manifest_sha256(
        segments,
        total_duration_ms=total_duration_ms,
        reason=reason,
    )
    job_id = hashlib.sha256(
        f"{uid}:{meeting_id}:{capture_run_id}:{manifest_sha256}".encode()
    ).hexdigest()

    def _run() -> CompletionResult:
        db = admin_firestore()
        meeting_ref = _meetings_ref(uid).document(meeting_id)
        run_ref = _capture_run_ref(uid, meeting_id, capture_run_id)
        segment_refs = [
            _segments_ref(uid, meeting_id, capture_run_id).document(f"{i:06d}")
            for i in range(len(segments))
        ]
        job_ref = _jobs_ref(uid).document(job_id)
        outbox_ref = _outbox_ref(uid).document(job_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> CompletionResult:
            meeting_snap = meeting_ref.get(transaction=txn)
            run_snap = run_ref.get(transaction=txn)
            segment_snaps = [ref.get(transaction=txn) for ref in segment_refs]
            if not meeting_snap.exists or not run_snap.exists:
                return CompletionResult(conflict_code="unknown_capture_run")
            meeting = meeting_snap.to_dict() or {}
            run = run_snap.to_dict() or {}
            if meeting.get(F.DELETION_STATE) or meeting.get(F.STATUS) in (
                F.STATUS_DELETE_REQUESTED,
                F.STATUS_DELETE_COMPLETE,
            ):
                return CompletionResult(conflict_code=F.FAIL_DELETION_IN_PROGRESS)
            if (
                int(meeting.get(F.CAPTURE_FENCE, -1)) != capture_fence
                or int(run.get(F.CAPTURE_FENCE, -1)) != capture_fence
            ):
                return CompletionResult(conflict_code=F.FAIL_STALE_CAPTURE_FENCE)

            original_receipt = meeting.get(F.COMPLETION_RECEIPT)
            original_manifest = meeting.get(F.MANIFEST_SHA256)
            if original_receipt:
                if original_manifest == manifest_sha256:
                    return CompletionResult(
                        receipt=original_receipt,
                        job_id=job_id,
                        idempotent=True,
                    )
                sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
                txn.update(meeting_ref, {F.AUDIT_SEQUENCE: sequence, F.UPDATED_AT: now})
                _audit_event(
                    txn,
                    uid=uid,
                    meeting_id=meeting_id,
                    sequence=sequence,
                    event_type="completion_rejected",
                    occurred_at=now,
                    actor_type="runtime",
                    actor_identity=runtime_instance_id or "unknown-runtime",
                    runtime_instance_id=runtime_instance_id,
                    capture_run_id=capture_run_id,
                    capture_fence=capture_fence,
                    prior_state=meeting.get(F.STATUS, ""),
                    next_state=meeting.get(F.STATUS, ""),
                    artifacts=[
                        {
                            "persisted_manifest_sha256": original_manifest,
                            "incoming_manifest_sha256": manifest_sha256,
                        }
                    ],
                    reason_code=F.FAIL_COMPLETION_CONFLICT,
                    correlation_id=correlation_id,
                )
                return CompletionResult(conflict_code=F.FAIL_COMPLETION_CONFLICT)

            # Ingest is closed by THIS transaction, not a prior one. A run that is
            # still ``capturing`` is the normal path; ``finalized`` is accepted so a
            # run closed by the previous two-transaction implementation can still
            # complete. Anything else (uploaded without a receipt, split_brain,
            # deleted) is a genuine integrity conflict.
            prior_run_state = str(run.get(F.CAPTURE_RUN_STATE, ""))
            code = ""
            if (
                prior_run_state
                not in (F.CAPTURE_RUN_CAPTURING, F.CAPTURE_RUN_FINALIZED)
                or segment_count != len(segments)
                or segment_count != len(segment_snaps)
                or [segment["seq"] for segment in segments] != list(range(len(segments)))
                # NOT checked: digest uniqueness. Two segments of pure silence
                # encode to byte-identical FLAC, so a real meeting with any two
                # quiet five-minute stretches produced duplicate digests and
                # could never complete - the audio uploaded fine and the note
                # never existed. A segment's identity is (seq, digest), never the
                # digest alone: each seq is bound to its own persisted document
                # and object path below, and manifest_sha256 covers the whole
                # ordered list, so duplicate content at distinct sequences is
                # both legitimate and unambiguous.
                or segment_digests != [segment["content_sha256"] for segment in segments]
                or computed_manifest != manifest_sha256
            ):
                code = F.FAIL_MANIFEST_INTEGRITY
            persisted: list[dict[str, Any]] = []
            if not code:
                for expected, snap in zip(segments, segment_snaps, strict=True):
                    if not snap.exists:
                        code = F.FAIL_MANIFEST_INTEGRITY
                        break
                    row = snap.to_dict() or {}
                    receipt = row.get("upload_receipt") or {}
                    if (
                        row.get("integrity_status") != "verified"
                        or any(
                            row.get(field) != expected[field]
                            for field in evidence.SEGMENT_IDENTITY_FIELDS
                        )
                        or receipt.get("content_sha256") != expected["content_sha256"]
                        or int(receipt.get("byte_length", -1)) != expected["byte_length"]
                        or not receipt.get("receipt_id")
                        or not receipt.get("object")
                        or not receipt.get("generation")
                    ):
                        code = F.FAIL_MANIFEST_INTEGRITY
                        break
                    persisted.append(row)
            # Compare against the captured SPAN, not the sum of segment
            # durations. They differ by exactly the silent gaps, and a device
            # reopen mid-meeting leaves a real one - a 52 second dropout at
            # capture start made a 30 minute meeting permanently uncompletable
            # even though every segment was present and verified.
            #
            # A gap is a QUALITY signal, and meeting-quality-v1 already scores it
            # (`unaccounted_gap`). This check exists to catch a client
            # misreporting its duration, and the span still does that.
            if segments:
                span = max(
                    segment["start_ms"] + segment["duration_ms"] for segment in segments
                ) - min(segment["start_ms"] for segment in segments)
            else:
                span = 0
            tolerance = evidence.duration_tolerance_ms(total_duration_ms)
            if not code and abs(span - total_duration_ms) > tolerance:
                code = F.FAIL_MANIFEST_INTEGRITY
            if code:
                return CompletionResult(conflict_code=code)

            receipt = {
                "receipt_id": uuid.uuid4().hex,
                F.MANIFEST_SHA256: manifest_sha256,
                "accepted_at": now,
            }
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            if prior_run_state == F.CAPTURE_RUN_CAPTURING:
                _audit_event(
                    txn,
                    uid=uid,
                    meeting_id=meeting_id,
                    sequence=sequence,
                    event_type="capture_finalized",
                    occurred_at=now,
                    actor_type="runtime",
                    actor_identity=runtime_instance_id or "unknown-runtime",
                    runtime_instance_id=runtime_instance_id,
                    capture_run_id=capture_run_id,
                    capture_fence=capture_fence,
                    prior_state=F.CAPTURE_RUN_CAPTURING,
                    next_state=F.CAPTURE_RUN_FINALIZED,
                    reason_code="completion_requested",
                    correlation_id=correlation_id,
                )
                sequence += 1
            txn.update(
                run_ref,
                {
                    F.CAPTURE_RUN_STATE: F.CAPTURE_RUN_UPLOADED,
                    F.MANIFEST_SHA256: manifest_sha256,
                    F.SEGMENT_COUNT: segment_count,
                    F.TOTAL_DURATION_MS: total_duration_ms,
                    F.COMPLETE_REASON: reason,
                    "finalized_at": now,
                    F.UPDATED_AT: now,
                },
            )
            for ref, expected in zip(segment_refs, segments, strict=True):
                txn.update(
                    ref,
                    {
                        "audio_metrics": expected["audio_metrics"],
                        F.UPDATED_AT: now,
                    },
                )
            txn.update(
                meeting_ref,
                {
                    F.STATUS: F.STATUS_UPLOADED,
                    F.PROCESSING_STAGE: F.STAGE_QUEUED,
                    F.MANIFEST_SHA256: manifest_sha256,
                    F.COMPLETION_RECEIPT: receipt,
                    F.SEGMENT_COUNT: segment_count,
                    F.TOTAL_DURATION_MS: total_duration_ms,
                    F.COMPLETE_REASON: reason,
                    F.UPDATED_AT: now,
                    F.STATUS_REVISION: gcloud_firestore.Increment(1),
                    F.AUDIT_SEQUENCE: sequence,
                },
            )
            job = {
                "job_id": job_id,
                "kind": "meeting_transcription_v2",
                "user_id": uid,
                "meeting_id": meeting_id,
                F.CAPTURE_RUN_ID: capture_run_id,
                F.CAPTURE_FENCE: capture_fence,
                F.MANIFEST_SHA256: manifest_sha256,
                "state": F.JOB_PENDING,
                "job_attempt": 0,
                "lease_token_hash": "",
                "lease_expires_at": "",
                "last_heartbeat_at": "",
                "dispatch_state": "pending",
                "dispatch_due_at": now,
                "created_at": now,
                "updated_at": now,
                "correlation_id": correlation_id,
                "causation_id": idempotency_key,
            }
            _txn_create(txn, job_ref, job)
            _txn_create(
                txn,
                outbox_ref,
                {
                    "outbox_id": job_id,
                    "job_id": job_id,
                    "user_id": uid,
                    "meeting_id": meeting_id,
                    "state": "pending",
                    "dispatch_due_at": now,
                    "dispatch_attempts": 0,
                    "created_at": now,
                    "updated_at": now,
                    "correlation_id": correlation_id,
                },
            )
            _audit_event(
                txn,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type="completion_verified",
                occurred_at=now,
                actor_type="runtime",
                actor_identity=runtime_instance_id or "unknown-runtime",
                runtime_instance_id=runtime_instance_id,
                capture_run_id=capture_run_id,
                capture_fence=capture_fence,
                job_id=job_id,
                prior_state=F.STATUS_CAPTURING,
                next_state=F.STATUS_UPLOADED,
                artifacts=[
                    {
                        "path": row["upload_receipt"]["object"],
                        "generation": row["upload_receipt"]["generation"],
                        "digest": row["content_sha256"],
                        "size": row["byte_length"],
                        "seq": row["seq"],
                    }
                    for row in persisted
                ],
                reason_code="completion_verified",
                correlation_id=correlation_id,
                causation_id=idempotency_key,
            )
            return CompletionResult(receipt=receipt, job_id=job_id)

        return _execute(transaction)

    result = await asyncio.to_thread(_run)
    if result.conflict_code == F.FAIL_STALE_CAPTURE_FENCE:
        raise StaleCaptureFenceError
    if result.conflict_code == F.FAIL_DELETION_IN_PROGRESS:
        raise DeletedMeetingError
    return result


async def transition_status(
    uid: str,
    meeting_id: str,
    *,
    from_statuses: tuple[str, ...],
    to_status: str,
    stage: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Transactional compare-and-set on ``status`` - the worker's idempotency
    primitive. Returns (transitioned, status_now); a doc already past the
    transition reports its current status so callers can treat re-runs as
    settled instead of failed. Every successful transition bumps
    ``status_revision`` and, when given, ``processing_stage``. Raises on
    Firestore failure."""

    def _run() -> tuple[bool, str]:
        db = admin_firestore()
        doc_ref = _meetings_ref(uid).document(meeting_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> tuple[bool, str]:
            snap = doc_ref.get(transaction=txn)
            if not snap.exists:
                return False, ""
            data = snap.to_dict() or {}
            current = data.get(F.STATUS, "")
            if current not in from_statuses:
                return False, current
            occurred_at = datetime.now(UTC).isoformat()
            sequence = int(data.get(F.AUDIT_SEQUENCE, 0)) + 1
            update: dict[str, Any] = {
                F.STATUS: to_status,
                F.UPDATED_AT: occurred_at,
                F.STATUS_REVISION: gcloud_firestore.Increment(1),
                F.AUDIT_SEQUENCE: sequence,
            }
            if stage is not None:
                update[F.PROCESSING_STAGE] = stage
            if extra:
                update.update(extra)
            txn.update(doc_ref, update)
            _audit_event(
                txn,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type="meeting_state_transitioned",
                occurred_at=occurred_at,
                runtime_instance_id=str(data.get(F.RUNTIME_INSTANCE_ID, "")),
                capture_run_id=str(data.get(F.CAPTURE_RUN_ID, "")),
                capture_fence=int(data.get(F.CAPTURE_FENCE, 0)),
                prior_state=current,
                next_state=to_status,
                reason_code=str((extra or {}).get(F.FAILURE_CODE, "")),
                policy_version=str(data.get(F.QUALITY_POLICY_VERSION, "")),
            )
            return True, to_status

        return _execute(transaction)

    transitioned, status_now = await asyncio.to_thread(_run)
    logger.info(
        "meetings.store: transition",
        {
            "user_id": uid,
            "meeting_id": meeting_id,
            "to": to_status,
            "transitioned": transitioned,
            "status_now": status_now,
        },
    )
    return transitioned, status_now


def failure_meta(*, code: str, retryable: bool, message: str = "") -> dict[str, Any]:
    """The failure fields to stamp alongside a transition into a terminal or
    recoverable-failed state. ``code`` is a safe FAIL_* enum, never a raw
    provider exception string."""
    return {
        F.FAILURE_CODE: code,
        F.FAILURE_MESSAGE: message,
        F.RETRYABLE: retryable,
        F.LAST_ERROR_AT: datetime.now(UTC).isoformat(),
    }


def clear_failure_meta() -> dict[str, Any]:
    """The inverse of failure_meta: drop the failure signal when a meeting is
    re-driven from a recoverable state (POST /meetings/{id}/retry)."""
    return {
        F.RETRYABLE: False,
        F.FAILURE_CODE: gcloud_firestore.DELETE_FIELD,
        F.FAILURE_MESSAGE: gcloud_firestore.DELETE_FIELD,
    }


async def record_upload_failure(uid: str, meeting_id: str, *, code: str) -> None:
    """Persist a safe upload problem without changing the coarse status.

    The encrypted desktop queue remains authoritative until /complete, so the
    meeting must continue accepting segment retries while the backend exposes a
    durable reason to newer clients.
    """
    update = {
        F.PROCESSING_STAGE: F.STAGE_UPLOADING,
        F.UPDATED_AT: datetime.now(UTC).isoformat(),
        **failure_meta(code=code, retryable=True),
    }
    await asyncio.to_thread(_meetings_ref(uid).document(meeting_id).update, update)


async def mark_failed(
    uid: str,
    meeting_id: str,
    *,
    from_statuses: tuple[str, ...],
    code: str,
    retryable: bool,
    message: str = "",
) -> tuple[bool, str]:
    """Transition to ``failed`` and stamp the safe failure metadata in one CAS."""
    return await transition_status(
        uid,
        meeting_id,
        from_statuses=from_statuses,
        to_status=F.STATUS_FAILED,
        extra=failure_meta(code=code, retryable=retryable, message=message),
    )


async def set_stage(uid: str, meeting_id: str, stage: str) -> None:
    """Mark a finer processing_stage WITHOUT a status change (e.g. transcribing
    -> building_insights inside one synthesizing lease). Best-effort: a failed
    stage marker must not abort the run it annotates."""

    def _update() -> None:
        _meetings_ref(uid).document(meeting_id).update(
            {
                F.PROCESSING_STAGE: stage,
                F.UPDATED_AT: datetime.now(UTC).isoformat(),
            }
        )

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn(
            "meetings.store: set_stage failed",
            {
                "user_id": uid,
                "meeting_id": meeting_id,
                "stage": stage,
                "error": str(exc),
            },
        )


def synthesis_lease_is_fresh(meeting: dict[str, Any], *, now_ms: int | None = None) -> bool:
    """Whether a synthesizing meeting is still owned by a live worker."""
    if meeting.get(F.STATUS) != F.STATUS_SYNTHESIZING:
        return False
    current_ms = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    started_ms = int(meeting.get(F.SYNTHESIS_STARTED_AT_MS, 0))
    return current_ms - started_ms < F.SYNTHESIS_LEASE_MS


async def claim_synthesis(uid: str, meeting_id: str) -> tuple[bool, str]:
    """Transactional synthesis lease. Grants the run when the meeting sits at
    "uploaded", or when a previous "synthesizing" claim is older than the
    lease (crashed worker). A concurrent Cloud Tasks duplicate arriving while
    a fresh lease is held is refused, so one meeting can never pay for STT+LLM
    twice at once. Returns (claimed, status_now). Raises on Firestore failure."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def _run() -> tuple[bool, str]:
        db = admin_firestore()
        doc_ref = _meetings_ref(uid).document(meeting_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> tuple[bool, str]:
            snap = doc_ref.get(transaction=txn)
            if not snap.exists:
                return False, ""
            data = snap.to_dict() or {}
            current = data.get(F.STATUS, "")
            lease_fresh = synthesis_lease_is_fresh(data, now_ms=now_ms)
            if current == F.STATUS_SYNTHESIZING and lease_fresh:
                return False, current
            if current not in (F.STATUS_UPLOADED, F.STATUS_SYNTHESIZING):
                return False, current
            sequence = int(data.get(F.AUDIT_SEQUENCE, 0)) + 1
            occurred_at = datetime.now(UTC).isoformat()
            txn.update(
                doc_ref,
                {
                    F.STATUS: F.STATUS_SYNTHESIZING,
                    F.SYNTHESIS_STARTED_AT_MS: now_ms,
                    F.UPDATED_AT: occurred_at,
                    F.PROCESSING_STAGE: F.STAGE_TRANSCRIBING,
                    F.ATTEMPT_COUNT: gcloud_firestore.Increment(1),
                    F.STATUS_REVISION: gcloud_firestore.Increment(1),
                    F.AUDIT_SEQUENCE: sequence,
                },
            )
            _audit_event(
                txn,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type="job_leased",
                occurred_at=occurred_at,
                runtime_instance_id=str(data.get(F.RUNTIME_INSTANCE_ID, "")),
                capture_run_id=str(data.get(F.CAPTURE_RUN_ID, "")),
                capture_fence=int(data.get(F.CAPTURE_FENCE, 0)),
                attempt=int(data.get(F.ATTEMPT_COUNT, 0)) + 1,
                prior_state=current,
                next_state=F.STATUS_SYNTHESIZING,
                reason_code="legacy_worker_lease",
            )
            return True, F.STATUS_SYNTHESIZING

        return _execute(transaction)

    claimed, status_now = await asyncio.to_thread(_run)
    logger.info(
        "meetings.store: synthesis claim",
        {
            "user_id": uid,
            "meeting_id": meeting_id,
            "claimed": claimed,
            "status_now": status_now,
        },
    )
    return claimed, status_now


@dataclass(frozen=True)
class JobLease:
    user_id: str
    job_id: str
    meeting_id: str
    capture_run_id: str
    capture_fence: int
    manifest_sha256: str
    job_attempt: int
    lease_token: str
    lease_expires_at: str
    correlation_id: str


def _lease_matches(job: dict[str, Any], lease: JobLease) -> bool:
    return (
        job.get("state") == F.JOB_LEASED
        and int(job.get("job_attempt", -1)) == lease.job_attempt
        and job.get("lease_token_hash") == _actor_hash(lease.lease_token)
        and int(job.get(F.CAPTURE_FENCE, -1)) == lease.capture_fence
    )


async def claim_job(uid: str, job_id: str) -> JobLease | None:
    """Acquire or steal an expired V2 job using a random, attempt-bound token."""
    now = datetime.now(UTC)
    expires = now + timedelta(milliseconds=F.SYNTHESIS_LEASE_MS)
    lease_token = uuid.uuid4().hex + uuid.uuid4().hex

    def _run() -> JobLease | None:
        db = admin_firestore()
        job_ref = _jobs_ref(uid).document(job_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> JobLease | None:
            job_snap = job_ref.get(transaction=txn)
            if not job_snap.exists:
                return None
            job = job_snap.to_dict() or {}
            meeting_id = str(job.get("meeting_id", ""))
            meeting_ref = _meetings_ref(uid).document(meeting_id)
            meeting_snap = meeting_ref.get(transaction=txn)
            if not meeting_snap.exists:
                return None
            meeting = meeting_snap.to_dict() or {}
            if meeting.get(F.DELETION_STATE) or meeting.get(F.STATUS) in (
                F.STATUS_DELETE_REQUESTED,
                F.STATUS_DELETE_COMPLETE,
            ):
                txn.update(job_ref, {"state": F.JOB_BLOCKED, "updated_at": now.isoformat()})
                return None
            state = job.get("state")
            lease_expiry = str(job.get("lease_expires_at", ""))
            if state == F.JOB_LEASED and lease_expiry > now.isoformat():
                return None
            if state not in (F.JOB_PENDING, F.JOB_DISPATCHED, F.JOB_RETRY, F.JOB_LEASED):
                return None
            if int(meeting.get(F.CAPTURE_FENCE, -1)) != int(
                job.get(F.CAPTURE_FENCE, -2)
            ) or meeting.get(F.MANIFEST_SHA256) != job.get(F.MANIFEST_SHA256):
                txn.update(
                    job_ref,
                    {
                        "state": F.JOB_BLOCKED,
                        "last_error_code": F.FAIL_STALE_CAPTURE_FENCE,
                        "updated_at": now.isoformat(),
                    },
                )
                return None
            attempt = int(job.get("job_attempt", 0)) + 1
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            txn.update(
                job_ref,
                {
                    "state": F.JOB_LEASED,
                    "job_attempt": attempt,
                    "lease_token_hash": _actor_hash(lease_token),
                    "lease_expires_at": expires.isoformat(),
                    "last_heartbeat_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                },
            )
            txn.update(
                meeting_ref,
                {
                    F.STATUS: F.STATUS_SYNTHESIZING,
                    F.PROCESSING_STAGE: F.STAGE_TRANSCRIBING,
                    F.ATTEMPT_COUNT: gcloud_firestore.Increment(1),
                    F.STATUS_REVISION: gcloud_firestore.Increment(1),
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now.isoformat(),
                },
            )
            _audit_event(
                txn,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type="job_leased" if state != F.JOB_LEASED else "job_stolen",
                occurred_at=now.isoformat(),
                capture_run_id=str(job.get(F.CAPTURE_RUN_ID, "")),
                capture_fence=int(job.get(F.CAPTURE_FENCE, 0)),
                job_id=job_id,
                attempt=attempt,
                lease_token=lease_token,
                prior_state=str(state),
                next_state=F.JOB_LEASED,
                reason_code="lease_acquired",
                correlation_id=str(job.get("correlation_id", "")),
            )
            return JobLease(
                user_id=uid,
                job_id=job_id,
                meeting_id=meeting_id,
                capture_run_id=str(job.get(F.CAPTURE_RUN_ID, "")),
                capture_fence=int(job.get(F.CAPTURE_FENCE, 0)),
                manifest_sha256=str(job.get(F.MANIFEST_SHA256, "")),
                job_attempt=attempt,
                lease_token=lease_token,
                lease_expires_at=expires.isoformat(),
                correlation_id=str(job.get("correlation_id", "")),
            )

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def heartbeat_job(lease: JobLease) -> bool:
    now = datetime.now(UTC)
    expires = now + timedelta(milliseconds=F.SYNTHESIS_LEASE_MS)

    def _run() -> bool:
        db = admin_firestore()
        job_ref = _jobs_ref(lease.user_id).document(lease.job_id)
        meeting_ref = _meetings_ref(lease.user_id).document(lease.meeting_id)
        txn = db.transaction()

        @gcloud_firestore.transactional
        def _execute(transaction) -> bool:
            job_snap = job_ref.get(transaction=transaction)
            meeting_snap = meeting_ref.get(transaction=transaction)
            job = job_snap.to_dict() or {}
            meeting = meeting_snap.to_dict() or {}
            if not _lease_matches(job, lease) or meeting.get(F.DELETION_STATE):
                return False
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            transaction.update(
                job_ref,
                {
                    "last_heartbeat_at": now.isoformat(),
                    "lease_expires_at": expires.isoformat(),
                    "updated_at": now.isoformat(),
                },
            )
            transaction.update(meeting_ref, {F.AUDIT_SEQUENCE: sequence})
            _audit_event(
                transaction,
                uid=lease.user_id,
                meeting_id=lease.meeting_id,
                sequence=sequence,
                event_type="job_heartbeated",
                occurred_at=now.isoformat(),
                capture_run_id=lease.capture_run_id,
                capture_fence=lease.capture_fence,
                job_id=lease.job_id,
                attempt=lease.job_attempt,
                lease_token=lease.lease_token,
                prior_state=F.JOB_LEASED,
                next_state=F.JOB_LEASED,
                correlation_id=lease.correlation_id,
            )
            return True

        return _execute(txn)

    return await asyncio.to_thread(_run)


async def get_job_context(lease: JobLease) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load the signed meeting and deterministic segment docs; never list GCS."""
    meeting = await get_meeting(lease.user_id, lease.meeting_id)
    if meeting is None:
        raise MeetingIntegrityError("meeting_missing", "Meeting disappeared.")
    count = int(meeting.get(F.SEGMENT_COUNT, 0))

    def _read_segments() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for seq in range(count):
            snap = (
                _segments_ref(
                    lease.user_id,
                    lease.meeting_id,
                    lease.capture_run_id,
                )
                .document(f"{seq:06d}")
                .get()
            )
            if not snap.exists:
                raise MeetingIntegrityError(
                    F.FAIL_MANIFEST_INTEGRITY,
                    f"Missing persisted segment {seq}.",
                )
            rows.append(snap.to_dict() or {})
        return rows

    rows = await asyncio.to_thread(_read_segments)
    if (
        int(meeting.get(F.CAPTURE_FENCE, -1)) != lease.capture_fence
        or meeting.get(F.MANIFEST_SHA256) != lease.manifest_sha256
    ):
        raise StaleCaptureFenceError
    return meeting, rows


async def get_job(uid: str, job_id: str) -> dict[str, Any] | None:
    def _read() -> dict[str, Any] | None:
        snap = _jobs_ref(uid).document(job_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    return await asyncio.to_thread(_read)


async def record_segment_attempt(
    lease: JobLease,
    *,
    seq: int,
    attempt_artifact: dict[str, Any],
    outcome: str,
    error_code: str = "",
) -> bool:
    """Commit per-segment worker progress only while the exact lease is current."""
    now = datetime.now(UTC).isoformat()

    def _run() -> bool:
        db = admin_firestore()
        job_ref = _jobs_ref(lease.user_id).document(lease.job_id)
        meeting_ref = _meetings_ref(lease.user_id).document(lease.meeting_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> bool:
            job_snap = job_ref.get(transaction=txn)
            meeting_snap = meeting_ref.get(transaction=txn)
            job = job_snap.to_dict() or {}
            meeting = meeting_snap.to_dict() or {}
            if not _lease_matches(job, lease) or meeting.get(F.DELETION_STATE):
                return False
            attempts = dict(job.get("segment_attempts") or {})
            previous = attempts.get(str(seq)) or {}
            history = list(previous.get("history") or [])
            history.append(
                {
                    "job_attempt": lease.job_attempt,
                    "outcome": outcome,
                    "error_code": error_code,
                    "artifact": attempt_artifact,
                    "updated_at": now,
                }
            )
            attempts[str(seq)] = {
                "job_attempt": lease.job_attempt,
                "outcome": outcome,
                "error_code": error_code,
                "artifact": attempt_artifact,
                "updated_at": now,
                "history": history,
            }
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            txn.update(job_ref, {"segment_attempts": attempts, "updated_at": now})
            txn.update(meeting_ref, {F.AUDIT_SEQUENCE: sequence})
            _audit_event(
                txn,
                uid=lease.user_id,
                meeting_id=lease.meeting_id,
                sequence=sequence,
                event_type="provider_responded" if outcome == "succeeded" else "provider_failed",
                occurred_at=now,
                capture_run_id=lease.capture_run_id,
                capture_fence=lease.capture_fence,
                job_id=lease.job_id,
                attempt=lease.job_attempt,
                lease_token=lease.lease_token,
                prior_state=F.STAGE_TRANSCRIBING,
                next_state=F.STAGE_TRANSCRIBING,
                artifacts=[attempt_artifact],
                reason_code=error_code,
                correlation_id=lease.correlation_id,
            )
            return True

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def fail_job(
    lease: JobLease,
    *,
    error_code: str,
    retryable: bool,
    max_attempts: int = 3,
) -> bool:
    """Fenced failure commit with durable full-jitter redispatch state."""
    now = datetime.now(UTC)
    should_retry = retryable and lease.job_attempt < max_attempts
    delay_s = random.uniform(0, min(3600, 30 * (2 ** max(lease.job_attempt - 1, 0))))
    due = now + timedelta(seconds=delay_s)

    def _run() -> bool:
        db = admin_firestore()
        job_ref = _jobs_ref(lease.user_id).document(lease.job_id)
        outbox_ref = _outbox_ref(lease.user_id).document(lease.job_id)
        meeting_ref = _meetings_ref(lease.user_id).document(lease.meeting_id)
        txn = db.transaction()

        @gcloud_firestore.transactional
        def _execute(transaction) -> bool:
            job_snap = job_ref.get(transaction=transaction)
            meeting_snap = meeting_ref.get(transaction=transaction)
            job = job_snap.to_dict() or {}
            meeting = meeting_snap.to_dict() or {}
            if not _lease_matches(job, lease) or meeting.get(F.DELETION_STATE):
                return False
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            next_job_state = F.JOB_RETRY if should_retry else F.JOB_FAILED
            transaction.update(
                job_ref,
                {
                    "state": next_job_state,
                    "last_error_code": error_code,
                    "next_attempt_at": due.isoformat() if should_retry else "",
                    "lease_token_hash": "",
                    "lease_expires_at": "",
                    "updated_at": now.isoformat(),
                },
            )
            if should_retry:
                transaction.update(
                    outbox_ref,
                    {
                        "state": "retry",
                        "dispatch_due_at": due.isoformat(),
                        "updated_at": now.isoformat(),
                    },
                )
                transaction.update(
                    meeting_ref,
                    {
                        F.STATUS: F.STATUS_UPLOADED,
                        F.PROCESSING_STAGE: F.STAGE_QUEUED,
                        F.FAILURE_CODE: error_code,
                        F.RETRYABLE: True,
                        F.LAST_ERROR_AT: now.isoformat(),
                        F.AUDIT_SEQUENCE: sequence,
                        F.UPDATED_AT: now.isoformat(),
                    },
                )
            else:
                transaction.update(
                    meeting_ref,
                    {
                        F.STATUS: F.STATUS_NEEDS_ATTENTION,
                        F.PROCESSING_STAGE: F.STAGE_NEEDS_ATTENTION,
                        **failure_meta(code=error_code, retryable=False),
                        F.AUDIT_SEQUENCE: sequence,
                        F.UPDATED_AT: now.isoformat(),
                    },
                )
            _audit_event(
                transaction,
                uid=lease.user_id,
                meeting_id=lease.meeting_id,
                sequence=sequence,
                event_type="job_retry_scheduled" if should_retry else "job_failed",
                occurred_at=now.isoformat(),
                capture_run_id=lease.capture_run_id,
                capture_fence=lease.capture_fence,
                job_id=lease.job_id,
                attempt=lease.job_attempt,
                lease_token=lease.lease_token,
                prior_state=F.JOB_LEASED,
                next_state=next_job_state,
                reason_code=error_code,
                correlation_id=lease.correlation_id,
            )
            return True

        return _execute(txn)

    return await asyncio.to_thread(_run)


async def retry_v2_job(uid: str, meeting_id: str) -> str:
    """Re-drive the authoritative job for a meeting needing attention."""
    now = datetime.now(UTC).isoformat()

    def _run() -> str:
        db = admin_firestore()
        meeting_ref = _meetings_ref(uid).document(meeting_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> str:
            meeting_snap = meeting_ref.get(transaction=txn)
            if not meeting_snap.exists:
                return ""
            meeting = meeting_snap.to_dict() or {}
            if meeting.get(F.DELETION_STATE):
                return ""
            manifest = str(meeting.get(F.MANIFEST_SHA256, ""))
            run_id = str(meeting.get(F.CAPTURE_RUN_ID, ""))
            if not manifest or not run_id:
                return ""
            job_id = hashlib.sha256(f"{uid}:{meeting_id}:{run_id}:{manifest}".encode()).hexdigest()
            job_ref = _jobs_ref(uid).document(job_id)
            outbox_ref = _outbox_ref(uid).document(job_id)
            job_snap = job_ref.get(transaction=txn)
            outbox_snap = outbox_ref.get(transaction=txn)
            job = job_snap.to_dict() or {}
            if (
                not job
                or job.get("state") not in (F.JOB_FAILED, F.JOB_RETRY)
                or meeting.get(F.STATUS) not in (F.STATUS_NEEDS_ATTENTION, F.STATUS_FAILED)
            ):
                return ""
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            txn.update(
                job_ref,
                {
                    "state": F.JOB_RETRY,
                    "lease_token_hash": "",
                    "lease_expires_at": "",
                    "next_attempt_at": now,
                    "updated_at": now,
                },
            )
            if outbox_snap.exists:
                txn.update(
                    outbox_ref,
                    {
                        "state": "retry",
                        "dispatch_due_at": now,
                        "updated_at": now,
                    },
                )
            else:
                _txn_create(
                    txn,
                    outbox_ref,
                    {
                        "outbox_id": job_id,
                        "job_id": job_id,
                        "user_id": uid,
                        "meeting_id": meeting_id,
                        "state": "retry",
                        "dispatch_due_at": now,
                        "dispatch_attempts": 0,
                        "created_at": now,
                        "updated_at": now,
                        "correlation_id": job.get("correlation_id", ""),
                    },
                )
            txn.update(
                meeting_ref,
                {
                    F.STATUS: F.STATUS_UPLOADED,
                    F.PROCESSING_STAGE: F.STAGE_QUEUED,
                    F.RETRYABLE: False,
                    F.FAILURE_CODE: gcloud_firestore.DELETE_FIELD,
                    F.FAILURE_MESSAGE: gcloud_firestore.DELETE_FIELD,
                    F.AUDIT_SEQUENCE: sequence,
                    F.STATUS_REVISION: gcloud_firestore.Increment(1),
                    F.UPDATED_AT: now,
                },
            )
            _audit_event(
                txn,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type="job_retry_requested",
                occurred_at=now,
                capture_run_id=run_id,
                capture_fence=int(meeting.get(F.CAPTURE_FENCE, 0)),
                job_id=job_id,
                prior_state=str(job.get("state", "")),
                next_state=F.JOB_RETRY,
                reason_code="user_retry",
                correlation_id=str(job.get("correlation_id", "")),
            )
            return job_id

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def publish_v2_result(
    lease: JobLease,
    *,
    expected_revision: int,
    artifacts: dict[str, Any],
    quality_report: dict[str, Any],
    note: dict[str, Any] | None,
    effective_tier: str,
) -> bool:
    """Fenced final commit. `ready` is impossible without passing immutable evidence."""
    now = datetime.now(UTC)
    passed = quality_report.get("decision") == "quality_passed"
    verified_silence = quality_report.get("decision") == "verified_silence"
    next_status = F.STATUS_READY if passed or verified_silence else F.STATUS_NEEDS_ATTENTION
    next_stage = F.STAGE_READY if next_status == F.STATUS_READY else F.STAGE_NEEDS_ATTENTION

    def _run() -> bool:
        db = admin_firestore()
        job_ref = _jobs_ref(lease.user_id).document(lease.job_id)
        meeting_ref = _meetings_ref(lease.user_id).document(lease.meeting_id)
        txn = db.transaction()

        @gcloud_firestore.transactional
        def _execute(transaction) -> bool:
            job_snap = job_ref.get(transaction=transaction)
            meeting_snap = meeting_ref.get(transaction=transaction)
            job = job_snap.to_dict() or {}
            meeting = meeting_snap.to_dict() or {}
            if (
                not _lease_matches(job, lease)
                or meeting.get(F.DELETION_STATE)
                or int(meeting.get(F.ARTIFACT_REVISION, 0)) != expected_revision
                or int(meeting.get(F.CAPTURE_FENCE, -1)) != lease.capture_fence
            ):
                return False
            if next_status == F.STATUS_READY:
                required = ("canonical", "webvtt", "quality_report", "note_input")
                if not all(
                    artifacts.get(name, {}).get("path")
                    and artifacts.get(name, {}).get("generation")
                    and artifacts.get(name, {}).get("sha256")
                    for name in required
                ):
                    return False
                if not (
                    passed
                    or (
                        verified_silence
                        and quality_report.get("audio_metrics_support_silence") is True
                        and quality_report.get("vad_supports_silence") is True
                    )
                ):
                    return False
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            revision = expected_revision + 1
            update: dict[str, Any] = {
                F.STATUS: next_status,
                F.PROCESSING_STAGE: next_stage,
                F.ARTIFACT_REVISION: revision,
                F.ARTIFACTS: artifacts,
                F.QUALITY_OUTCOME: quality_report.get("decision", ""),
                F.QUALITY_POLICY_VERSION: F.QUALITY_POLICY_V1,
                F.UPDATED_AT: now.isoformat(),
                F.STATUS_REVISION: gcloud_firestore.Increment(1),
                F.AUDIT_SEQUENCE: sequence,
                F.RETRYABLE: next_status != F.STATUS_READY,
            }
            if next_status == F.STATUS_READY:
                update[F.NOTE] = note
                update[F.FAILURE_CODE] = gcloud_firestore.DELETE_FIELD
                update[F.FAILURE_MESSAGE] = gcloud_firestore.DELETE_FIELD
                if effective_tier != "pro":
                    update[F.EXPIRES_AT] = now + timedelta(days=F.RETENTION_DAYS)
            else:
                update.update(
                    failure_meta(
                        code=F.FAIL_TRANSCRIPT_QUALITY,
                        retryable=True,
                        message="Transcript evidence needs attention.",
                    )
                )
            transaction.update(meeting_ref, update)
            transaction.update(
                job_ref,
                {
                    "state": F.JOB_COMPLETE if next_status == F.STATUS_READY else F.JOB_FAILED,
                    "quality_decision": quality_report.get("decision", ""),
                    "artifact_revision": revision,
                    "lease_token_hash": "",
                    "lease_expires_at": "",
                    "updated_at": now.isoformat(),
                },
            )
            _audit_event(
                transaction,
                uid=lease.user_id,
                meeting_id=lease.meeting_id,
                sequence=sequence,
                event_type="note_published" if next_status == F.STATUS_READY else "quality_failed",
                occurred_at=now.isoformat(),
                capture_run_id=lease.capture_run_id,
                capture_fence=lease.capture_fence,
                job_id=lease.job_id,
                attempt=lease.job_attempt,
                lease_token=lease.lease_token,
                prior_state=F.STATUS_SYNTHESIZING,
                next_state=next_status,
                artifacts=list(artifacts.values()),
                reason_code=str(quality_report.get("decision", "")),
                correlation_id=lease.correlation_id,
                policy_version=F.QUALITY_POLICY_V1,
            )
            return True

        return _execute(txn)

    return await asyncio.to_thread(_run)


async def save_note(
    uid: str,
    meeting_id: str,
    note: dict[str, Any],
    *,
    effective_tier: str,
) -> None:
    """Persist the synthesized note and flip status to ready. Non-pro notes get
    the RETENTION_DAYS TTL stamp; pro notes carry no expiry (full history is
    the paid feature). Raises on failure so the worker marks the run failed
    instead of deleting audio for a note that never landed."""
    now = datetime.now(UTC)
    update: dict[str, Any] = {
        F.NOTE: note,
        F.STATUS: F.STATUS_READY,
        F.UPDATED_AT: now.isoformat(),
        F.PROCESSING_STAGE: F.STAGE_READY,
        F.STATUS_REVISION: gcloud_firestore.Increment(1),
        # A successful (re)run clears any earlier failure signal.
        F.RETRYABLE: False,
        F.FAILURE_CODE: gcloud_firestore.DELETE_FIELD,
        F.FAILURE_MESSAGE: gcloud_firestore.DELETE_FIELD,
    }
    if effective_tier != "pro":
        update[F.EXPIRES_AT] = now + timedelta(days=F.RETENTION_DAYS)

    def _run() -> None:
        db = admin_firestore()
        doc_ref = _meetings_ref(uid).document(meeting_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> None:
            snap = doc_ref.get(transaction=txn)
            if not snap.exists:
                raise MeetingIntegrityError("meeting_missing", "Meeting disappeared.")
            meeting = snap.to_dict() or {}
            sequence = int(meeting.get(F.AUDIT_SEQUENCE, 0)) + 1
            update[F.AUDIT_SEQUENCE] = sequence
            txn.update(doc_ref, update)
            _audit_event(
                txn,
                uid=uid,
                meeting_id=meeting_id,
                sequence=sequence,
                event_type="note_published",
                occurred_at=now.isoformat(),
                runtime_instance_id=str(meeting.get(F.RUNTIME_INSTANCE_ID, "")),
                capture_run_id=str(meeting.get(F.CAPTURE_RUN_ID, "")),
                capture_fence=int(meeting.get(F.CAPTURE_FENCE, 0)),
                prior_state=str(meeting.get(F.STATUS, "")),
                next_state=F.STATUS_READY,
                reason_code="legacy_note_published",
            )

        _execute(transaction)

    await asyncio.to_thread(_run)
    logger.info(
        "meetings.store: note saved",
        {
            "user_id": uid,
            "meeting_id": meeting_id,
            "tier": effective_tier,
            "summary_chars": len(note.get("summary", "")),
            "action_items": len(note.get("action_items", [])),
            "transcript_turns": len(note.get(F.NOTE_TRANSCRIPT, [])),
            "transcript_chars": sum(
                len(turn.get(F.TRANSCRIPT_TEXT, ""))
                for turn in note.get(F.NOTE_TRANSCRIPT, [])
                if isinstance(turn, dict)
            ),
        },
    )


async def list_recent(uid: str, *, limit: int = F.LIST_LIMIT) -> list[dict[str, Any]]:
    """Recent meetings, newest first, expired rows dropped (TTL sweeper can lag
    ~72h). Fails closed to an empty list, matching drafts' read path."""
    if not uid:
        return []
    limit = max(1, min(limit, F.LIST_LIMIT))

    def _read() -> list[dict[str, Any]]:
        query = _meetings_ref(uid).order_by(F.CREATED_AT, direction="DESCENDING").limit(limit)
        now = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            expires_at = data.get(F.EXPIRES_AT)
            if expires_at is not None and expires_at < now:
                continue
            data["meeting_id"] = snap.id
            rows.append(data)
        return rows

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("meetings.store: list failed", {"user_id": uid, "error": str(exc)})
        return []


async def get_exclude_keywords(uid: str) -> list[str]:
    """The user's sensitive-meeting exclude list. An absent doc is an empty
    list; a READ FAILURE raises. Failing open here would send a meeting the
    user explicitly excluded to a third-party STT vendor because of a
    transient Firestore blip - an irreversible disclosure. The caller treats
    the raise as retryable infrastructure (audio stays put, the task retries)."""

    def _read() -> list[str]:
        snap = (
            admin_firestore()
            .collection(F.PARENT_COLLECTION)
            .document(uid)
            .collection(F.SETTINGS_SUBCOLLECTION)
            .document(F.SETTINGS_DOC)
            .get()
        )
        raw = (snap.to_dict() or {}).get("exclude_keywords", [])
        return [str(k).strip().lower() for k in raw if str(k).strip()]

    return await asyncio.to_thread(_read)
