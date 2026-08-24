"""Create-only Cloud Storage for Meeting Recording V2 evidence and artifacts.

V2 object layouts:

    audio/v2/{uid}/{meeting_id}/{capture_run_id}/{seq:06d}/{sha256}.flac
    transcripts/v2/{uid}/{meeting_id}/attempts/{attempt_id}/segments/{seq}.json
    transcripts/v2/{uid}/{meeting_id}/revisions/{revision_id}/{artifact}

All V2 writes use generation-match zero. An existing object is accepted only
when its immutable identity, digest, and size match; mismatches are terminal
split-brain evidence. Successful transcription and note publication never
delete source audio. Explicit deletion targets the recorded object path and
    generation, never a broad prefix.

Same module shape as services/gcs.py (lazy client singleton, every blocking
call in ``asyncio.to_thread``). Audio and transcript bucket names come from
``MEETINGS_AUDIO_BUCKET`` and ``MEETINGS_TRANSCRIPT_BUCKET``.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from ...lib.logger import logger
from .evidence import canonical_json_bytes, sha256_hex

_client_singleton: Any = None


def _client() -> Any:
    global _client_singleton
    if _client_singleton is None:
        from google.cloud import storage  # type: ignore

        _client_singleton = storage.Client()
    return _client_singleton


def bucket_name() -> str:
    return os.getenv("MEETINGS_AUDIO_BUCKET", "juno-2ea45-meeting-audio")


def transcript_bucket_name() -> str:
    return os.getenv("MEETINGS_TRANSCRIPT_BUCKET", bucket_name())


@dataclass(frozen=True)
class ImmutableObject:
    path: str
    generation: str
    size: int
    sha256: str
    crc32c: str | None
    etag: str | None
    content_type: str
    reconciled: bool = False


class ImmutableObjectConflict(RuntimeError):
    def __init__(self, path: str):
        super().__init__(f"Immutable object identity conflict at {path}")
        self.path = path


def v2_object_path_for(
    uid: str,
    meeting_id: str,
    capture_run_id: str,
    seq: int,
    content_sha256: str,
) -> str:
    return f"audio/v2/{uid}/{meeting_id}/{capture_run_id}/{seq:06d}/{content_sha256}.flac"


def transcript_attempt_path(
    uid: str,
    meeting_id: str,
    attempt_id: str,
    seq: int,
) -> str:
    return f"transcripts/v2/{uid}/{meeting_id}/attempts/{attempt_id}/segments/{seq}.json"


def transcript_revision_path(
    uid: str,
    meeting_id: str,
    revision_id: str,
    filename: str,
) -> str:
    return f"transcripts/v2/{uid}/{meeting_id}/revisions/{revision_id}/{filename}"


async def create_v2_segment(
    uid: str,
    meeting_id: str,
    capture_run_id: str,
    seq: int,
    content_sha256: str,
    data: bytes,
    *,
    metadata: dict[str, str],
) -> ImmutableObject:
    """Create or reconcile one content-addressed FLAC object.

    A generation-zero precondition is mandatory.  The only accepted
    precondition failure is an exact metadata, digest, and size match.
    """
    path = v2_object_path_for(uid, meeting_id, capture_run_id, seq, content_sha256)
    required = {
        **metadata,
        "uid": uid,
        "meeting_id": meeting_id,
        "capture_run_id": capture_run_id,
        "seq": str(seq),
        "content_sha256": content_sha256,
        "byte_length": str(len(data)),
        "schema_version": "2",
    }

    def _snapshot(blob: Any, *, reconciled: bool) -> ImmutableObject:
        actual = blob.metadata or {}
        if int(blob.size or -1) != len(data) or any(
            str(actual.get(key, "")) != value for key, value in required.items()
        ):
            raise ImmutableObjectConflict(path)
        return ImmutableObject(
            path=path,
            generation=str(blob.generation),
            size=int(blob.size),
            sha256=content_sha256,
            crc32c=getattr(blob, "crc32c", None),
            etag=getattr(blob, "etag", None),
            content_type=str(getattr(blob, "content_type", None) or "audio/flac"),
            reconciled=reconciled,
        )

    def _create() -> ImmutableObject:
        from google.api_core.exceptions import PreconditionFailed  # type: ignore

        blob = _client().bucket(bucket_name()).blob(path)
        blob.metadata = required
        try:
            blob.upload_from_string(
                data,
                content_type="audio/flac",
                if_generation_match=0,
                checksum="auto",
            )
            blob.reload()
            return _snapshot(blob, reconciled=False)
        except PreconditionFailed:
            existing = _client().bucket(bucket_name()).get_blob(path)
            if existing is None:
                raise
            return _snapshot(existing, reconciled=True)

    result = await asyncio.to_thread(_create)
    logger.info(
        "meetings.gcs: immutable segment accepted",
        {
            "meeting_id": meeting_id,
            "capture_run_id": capture_run_id,
            "capture_fence": metadata.get("capture_fence"),
            "seq": seq,
            "content_sha256": content_sha256,
            "byte_length": len(data),
            "object": result.path,
            "generation": result.generation,
            "reconciled": result.reconciled,
            "correlation_id": metadata.get("correlation_id"),
        },
    )
    return result


async def create_json_artifact(
    path: str,
    value: Any,
    *,
    metadata: dict[str, str],
) -> ImmutableObject:
    return await create_artifact(
        path,
        canonical_json_bytes(value),
        content_type="application/json",
        metadata=metadata,
    )


async def create_text_artifact(
    path: str,
    value: str,
    *,
    content_type: str,
    metadata: dict[str, str],
) -> ImmutableObject:
    return await create_artifact(
        path,
        value.encode("utf-8"),
        content_type=content_type,
        metadata=metadata,
    )


async def create_artifact(
    path: str,
    data: bytes,
    *,
    content_type: str,
    metadata: dict[str, str],
) -> ImmutableObject:
    digest = sha256_hex(data)
    required = {
        **metadata,
        "content_sha256": digest,
        "byte_length": str(len(data)),
    }

    def _snapshot(blob: Any, *, reconciled: bool) -> ImmutableObject:
        actual = blob.metadata or {}
        if int(blob.size or -1) != len(data) or any(
            str(actual.get(key, "")) != value for key, value in required.items()
        ):
            raise ImmutableObjectConflict(path)
        return ImmutableObject(
            path=path,
            generation=str(blob.generation),
            size=int(blob.size),
            sha256=digest,
            crc32c=getattr(blob, "crc32c", None),
            etag=getattr(blob, "etag", None),
            content_type=content_type,
            reconciled=reconciled,
        )

    def _create() -> ImmutableObject:
        from google.api_core.exceptions import PreconditionFailed  # type: ignore

        bucket = _client().bucket(transcript_bucket_name())
        blob = bucket.blob(path)
        blob.metadata = required
        try:
            blob.upload_from_string(
                data,
                content_type=content_type,
                if_generation_match=0,
                checksum="auto",
            )
            blob.reload()
            return _snapshot(blob, reconciled=False)
        except PreconditionFailed:
            existing = bucket.get_blob(path)
            if existing is None:
                raise
            return _snapshot(existing, reconciled=True)

    return await asyncio.to_thread(_create)


async def delete_exact_object(
    path: str,
    generation: str,
    *,
    transcript: bool = False,
) -> dict[str, Any]:
    """Delete exactly one recorded generation and return a durable receipt payload."""
    bucket = transcript_bucket_name() if transcript else bucket_name()

    def _delete() -> dict[str, Any]:
        from google.api_core.exceptions import NotFound  # type: ignore

        blob = _client().bucket(bucket).blob(path, generation=int(generation))
        try:
            blob.delete(if_generation_match=int(generation))
            outcome = "deleted"
        except NotFound:
            outcome = "already_absent"
        return {
            "object": path,
            "generation": str(generation),
            "outcome": outcome,
        }

    return await asyncio.to_thread(_delete)


async def download_exact(
    path: str,
    generation: str,
    *,
    transcript: bool = False,
) -> bytes:
    bucket = transcript_bucket_name() if transcript else bucket_name()

    def _download() -> bytes:
        blob = _client().bucket(bucket).blob(path, generation=int(generation))
        return blob.download_as_bytes(if_generation_match=int(generation))

    return await asyncio.to_thread(_download)


async def delete_user_audio(uid: str) -> int:
    """Strict account-deletion cleanup for every raw meeting object owned by a user.

    This enumerates owned objects and deletes each exact generation, raising on
    any storage failure. The account handler keeps Firebase Auth intact so the
    user can retry instead of reporting a completed deletion while audio
    remains. Partial deletion is safe because object deletes are idempotent and
    a retry lists only what remains.
    """
    prefix = f"audio/v2/{uid}/"

    def _delete() -> int:
        bucket = _client().bucket(bucket_name())
        blobs = list(_client().list_blobs(bucket_name(), prefix=prefix))
        for blob in blobs:
            bucket.blob(blob.name, generation=blob.generation).delete(
                if_generation_match=int(blob.generation),
            )
        return len(blobs)

    count = await asyncio.to_thread(_delete)
    logger.info(
        "meetings.gcs: user audio deleted",
        {
            "user_id": uid,
            "deleted": count,
        },
    )
    return count


async def delete_user_transcripts(uid: str) -> int:
    """Account deletion: enumerate, then delete each exact transcript generation."""
    prefix = f"transcripts/v2/{uid}/"

    def _delete() -> int:
        bucket = _client().bucket(transcript_bucket_name())
        blobs = list(_client().list_blobs(transcript_bucket_name(), prefix=prefix))
        for blob in blobs:
            bucket.blob(blob.name, generation=blob.generation).delete(
                if_generation_match=int(blob.generation),
            )
        return len(blobs)

    count = await asyncio.to_thread(_delete)
    logger.info(
        "meetings.gcs: user transcript artifacts deleted",
        {
            "deleted": count,
        },
    )
    return count
