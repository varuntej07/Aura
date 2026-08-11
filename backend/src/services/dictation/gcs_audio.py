"""Create-only Cloud Storage for opt-in dictation training audio."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from ...lib.logger import logger

_client_singleton: Any = None


def _client() -> Any:
    global _client_singleton
    if _client_singleton is None:
        from google.cloud import storage  # type: ignore

        _client_singleton = storage.Client()
    return _client_singleton


def bucket_name() -> str:
    return os.getenv("DICTATION_AUDIO_BUCKET", "juno-2ea45-dictation-audio")


def object_path_for(uid: str, trace_id: str, content_sha256: str) -> str:
    return f"dictation/v1/{uid}/{trace_id}/{content_sha256}.flac"


def user_prefix_for(uid: str) -> str:
    return f"dictation/v1/{uid}/"


@dataclass(frozen=True)
class ImmutableObject:
    path: str
    generation: str
    size: int
    sha256: str
    reconciled: bool


class ImmutableObjectConflict(RuntimeError):
    def __init__(self, path: str):
        super().__init__(f"Immutable object identity conflict at {path}")
        self.path = path


async def create_audio(
    uid: str,
    trace_id: str,
    content_sha256: str,
    data: bytes,
) -> ImmutableObject:
    path = object_path_for(uid, trace_id, content_sha256)
    required = {
        "trace_id": trace_id,
        "content_sha256": content_sha256,
        "byte_length": str(len(data)),
        "schema_version": "1",
    }

    def _snapshot(blob: Any, *, reconciled: bool) -> ImmutableObject:
        actual = blob.metadata or {}
        if (
            int(blob.size or -1) != len(data)
            or str(getattr(blob, "content_type", "")) != "audio/flac"
            or any(str(actual.get(key, "")) != value for key, value in required.items())
        ):
            raise ImmutableObjectConflict(path)
        return ImmutableObject(
            path=path,
            generation=str(blob.generation),
            size=int(blob.size),
            sha256=content_sha256,
            reconciled=reconciled,
        )

    def _create() -> ImmutableObject:
        from google.api_core.exceptions import PreconditionFailed  # type: ignore

        bucket = _client().bucket(bucket_name())
        blob = bucket.blob(path)
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
            existing = bucket.get_blob(path)
            if existing is None:
                raise
            return _snapshot(existing, reconciled=True)

    result = await asyncio.to_thread(_create)
    logger.info(
        "dictation.gcs: immutable audio accepted",
        {
            "trace_id": trace_id,
            "byte_length": len(data),
            "generation": result.generation,
            "reconciled": result.reconciled,
        },
    )
    return result


async def object_exists(path: str, generation: str) -> bool:
    def _exists() -> bool:
        blob = _client().bucket(bucket_name()).get_blob(path, generation=int(generation))
        return blob is not None

    return await asyncio.to_thread(_exists)


async def current_generation(path: str) -> str | None:
    """Resolve the sole create-only generation when a receipt write crashed."""

    def _get() -> str | None:
        blob = _client().bucket(bucket_name()).get_blob(path)
        return str(blob.generation) if blob is not None else None

    return await asyncio.to_thread(_get)


async def download_exact(path: str, generation: str) -> bytes:
    def _download() -> bytes:
        blob = _client().bucket(bucket_name()).blob(path, generation=int(generation))
        return blob.download_as_bytes(if_generation_match=int(generation))

    return await asyncio.to_thread(_download)


async def delete_exact(path: str, generation: str) -> bool:
    """Delete one recorded generation. Missing is success for retry recovery."""

    def _delete() -> bool:
        from google.api_core.exceptions import NotFound  # type: ignore

        blob = _client().bucket(bucket_name()).blob(path, generation=int(generation))
        try:
            blob.delete(if_generation_match=int(generation))
            return True
        except NotFound:
            return False

    return await asyncio.to_thread(_delete)


async def delete_user_audio(uid: str) -> int:
    """Strict account deletion for every dictation object owned by one user."""
    prefix = user_prefix_for(uid)

    def _delete() -> int:
        bucket = _client().bucket(bucket_name())
        blobs = list(_client().list_blobs(bucket_name(), prefix=prefix))
        for blob in blobs:
            bucket.blob(blob.name, generation=blob.generation).delete(
                if_generation_match=int(blob.generation),
            )
        return len(blobs)

    count = await asyncio.to_thread(_delete)
    logger.info("dictation.gcs: user audio deleted", {"user_id": uid, "deleted": count})
    return count
