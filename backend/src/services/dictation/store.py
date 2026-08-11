"""Transactional Firestore state for dictation traces and monthly quotas."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from google.cloud import firestore as gcloud_firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F
from . import gcs_audio
from .models import TracePayload


def _trace_ref(uid: str, trace_id: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.TRACE_SUBCOLLECTION)
        .document(trace_id)
    )


def _usage_ref(uid: str, month_key: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.USAGE_SUBCOLLECTION)
        .document(f"dictation_{month_key}")
    )


def _month_window(now: datetime) -> tuple[str, int]:
    month_key = now.strftime("%Y%m")
    if now.month == 12:
        reset = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        reset = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    return month_key, int(reset.timestamp() * 1_000)


@dataclass(frozen=True)
class MetadataResult:
    status: Literal["created", "idempotent", "conflict", "deleted", "quota"]
    remaining: int
    resets_at_ms: int
    has_audio: bool = False


async def put_metadata(
    uid: str,
    trace_id: str,
    payload: TracePayload,
) -> MetadataResult:
    now = datetime.now(UTC)
    month_key, resets_at_ms = _month_window(now)
    fingerprint = payload.fingerprint(trace_id)
    metadata = payload.normalized_dict(trace_id)

    def _run() -> MetadataResult:
        db = admin_firestore()
        trace_ref = _trace_ref(uid, trace_id)
        usage_ref = _usage_ref(uid, month_key)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> MetadataResult:
            trace_snap = trace_ref.get(transaction=txn)
            usage_snap = usage_ref.get(transaction=txn)
            count = int((usage_snap.to_dict() or {}).get("count", 0))
            remaining = max(0, F.MONTHLY_TRACE_CAP - count)

            if trace_snap.exists:
                current = trace_snap.to_dict() or {}
                if current.get(F.DELETED_AT) or current.get(F.DELETION_STATE):
                    return MetadataResult("deleted", remaining, resets_at_ms)
                if current.get(F.METADATA_SHA256) != fingerprint:
                    return MetadataResult("conflict", remaining, resets_at_ms)
                return MetadataResult(
                    "idempotent",
                    remaining,
                    resets_at_ms,
                    bool(current.get(F.HAS_AUDIO, False)),
                )

            if count >= F.MONTHLY_TRACE_CAP:
                return MetadataResult("quota", 0, resets_at_ms)

            txn.set(
                trace_ref,
                {
                    **metadata,
                    F.TRACE_ID: trace_id,
                    F.METADATA_SHA256: fingerprint,
                    F.UPLOADED_AT: now,
                    F.HAS_AUDIO: False,
                    F.AUDIO_PATH: None,
                    F.AUDIO_GENERATION: None,
                    F.AUDIO_UPLOADED_AT: None,
                    F.AUDIO_EXPIRES_AT: None,
                    F.DELETION_STATE: None,
                    F.DELETED_AT: None,
                    F.QUOTA_MONTH: month_key,
                },
            )
            txn.set(
                usage_ref,
                {
                    "count": count + 1,
                    "month": month_key,
                    "resets_at_ms": resets_at_ms,
                    "updated_at": now,
                },
            )
            return MetadataResult(
                "created",
                F.MONTHLY_TRACE_CAP - count - 1,
                resets_at_ms,
            )

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def get_trace(uid: str, trace_id: str) -> dict | None:
    def _get() -> dict | None:
        snap = _trace_ref(uid, trace_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    return await asyncio.to_thread(_get)


@dataclass(frozen=True)
class AttachResult:
    status: Literal["attached", "idempotent", "missing", "deleted", "conflict"]


async def attach_audio(
    uid: str,
    trace_id: str,
    *,
    path: str,
    generation: str,
    content_sha256: str,
    byte_length: int,
) -> AttachResult:
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=F.AUDIO_RETENTION_DAYS)

    def _run() -> AttachResult:
        db = admin_firestore()
        trace_ref = _trace_ref(uid, trace_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> AttachResult:
            snap = trace_ref.get(transaction=txn)
            if not snap.exists:
                return AttachResult("missing")
            current = snap.to_dict() or {}
            if current.get(F.DELETED_AT) or current.get(F.DELETION_STATE):
                return AttachResult("deleted")
            if (
                current.get("audioSha256") != content_sha256
                or int(current.get("audioBytes", -1)) != byte_length
            ):
                return AttachResult("conflict")

            existing_path = current.get(F.AUDIO_PATH)
            existing_generation = current.get(F.AUDIO_GENERATION)
            if existing_path:
                if existing_path == path and str(existing_generation) == str(generation):
                    return AttachResult("idempotent")
                return AttachResult("conflict")

            txn.update(
                trace_ref,
                {
                    F.HAS_AUDIO: True,
                    F.AUDIO_PATH: path,
                    F.AUDIO_GENERATION: str(generation),
                    F.AUDIO_UPLOADED_AT: now,
                    F.AUDIO_EXPIRES_AT: expires_at,
                },
            )
            return AttachResult("attached")

        return _execute(transaction)

    return await asyncio.to_thread(_run)


@dataclass(frozen=True)
class DeleteTarget:
    status: Literal["absent", "tombstoned", "pending"]
    path: str | None = None
    generation: str | None = None
    content_sha256: str | None = None


async def begin_delete(uid: str, trace_id: str) -> DeleteTarget:
    now = datetime.now(UTC)

    def _run() -> DeleteTarget:
        db = admin_firestore()
        trace_ref = _trace_ref(uid, trace_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> DeleteTarget:
            snap = trace_ref.get(transaction=txn)
            if not snap.exists:
                return DeleteTarget("absent")
            current = snap.to_dict() or {}
            if current.get(F.DELETED_AT):
                return DeleteTarget("tombstoned")
            if current.get(F.DELETION_STATE) != F.DELETION_PENDING:
                txn.update(
                    trace_ref,
                    {
                        F.DELETION_STATE: F.DELETION_PENDING,
                        F.DELETION_REQUESTED_AT: now,
                    },
                )
            return DeleteTarget(
                "pending",
                current.get(F.AUDIO_PATH),
                str(current.get(F.AUDIO_GENERATION))
                if current.get(F.AUDIO_GENERATION) is not None
                else None,
                current.get("audioSha256"),
            )

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def finish_delete(uid: str, trace_id: str) -> None:
    now = datetime.now(UTC)

    def _run() -> None:
        trace_ref = _trace_ref(uid, trace_id)
        trace_ref.set(
            {
                F.TRACE_ID: trace_id,
                F.HAS_AUDIO: False,
                F.DELETION_STATE: F.DELETION_COMPLETE,
                F.DELETED_AT: now,
            }
        )

    await asyncio.to_thread(_run)


async def get_quota(uid: str) -> tuple[int, int]:
    now = datetime.now(UTC)
    month_key, resets_at_ms = _month_window(now)

    def _get() -> int:
        snap = _usage_ref(uid, month_key).get()
        return int((snap.to_dict() or {}).get("count", 0)) if snap.exists else 0

    count = await asyncio.to_thread(_get)
    return max(0, F.MONTHLY_TRACE_CAP - count), resets_at_ms


async def mark_audio_missing(reference, *, path: str, generation: str) -> bool:
    """Clear a stale audio marker only when the recorded receipt still matches."""
    now = datetime.now(UTC)

    def _run() -> bool:
        db = admin_firestore()
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn) -> bool:
            snap = reference.get(transaction=txn)
            current = snap.to_dict() or {}
            if (
                not snap.exists
                or not current.get(F.HAS_AUDIO)
                or current.get(F.AUDIO_PATH) != path
                or str(current.get(F.AUDIO_GENERATION)) != str(generation)
            ):
                return False
            txn.update(
                reference,
                {
                    F.HAS_AUDIO: False,
                    F.AUDIO_EXPIRES_AT: gcloud_firestore.DELETE_FIELD,
                    "audio_missing_confirmed_at": now,
                },
            )
            return True

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def reconcile_expired_audio(limit: int = F.RECONCILE_BATCH_LIMIT) -> dict[str, int]:
    """Confirm lifecycle deletions and prevent exports from referencing dead blobs."""
    now = datetime.now(UTC)

    def _query():
        return list(
            admin_firestore()
            .collection_group(F.TRACE_SUBCOLLECTION)
            .where(filter=FieldFilter(F.AUDIO_EXPIRES_AT, "<=", now))
            .limit(limit)
            .stream()
        )

    snapshots = await asyncio.to_thread(_query)
    semaphore = asyncio.Semaphore(20)

    async def _reconcile_one(snapshot) -> tuple[int, int]:
        current = snapshot.to_dict() or {}
        path = current.get(F.AUDIO_PATH)
        generation = current.get(F.AUDIO_GENERATION)
        if not current.get(F.HAS_AUDIO) or not path or generation is None:
            return 0, 0
        async with semaphore:
            if await gcs_audio.object_exists(path, str(generation)):
                return 1, 0
            changed = await mark_audio_missing(
                snapshot.reference,
                path=path,
                generation=str(generation),
            )
            return 1, int(changed)

    results = await asyncio.gather(*(_reconcile_one(snap) for snap in snapshots))
    checked = sum(item[0] for item in results)
    marked_missing = sum(item[1] for item in results)
    logger.info(
        "dictation: audio lifecycle reconciliation complete",
        {"candidates": len(snapshots), "checked": checked, "marked_missing": marked_missing},
    )
    return {"candidates": len(snapshots), "checked": checked, "marked_missing": marked_missing}
