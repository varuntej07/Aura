"""Create-only Cloud Storage for opt-in dictation training audio.

Storage mechanics (client singleton, create-or-reconcile, generation-pinned
reads and deletes) live in services/immutable_gcs.py; this module owns the
dictation paths, metadata composition, result shape, and log lines.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ...config.settings import settings
from ...lib.logger import logger
from .. import immutable_gcs


def bucket_name() -> str:
    return settings.DICTATION_AUDIO_BUCKET


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

    blob, reconciled = await immutable_gcs.create_or_reconcile(
        bucket_name=bucket_name(),
        path=path,
        data=data,
        content_type="audio/flac",
        required_metadata=required,
        verify_content_type=True,
        make_conflict=lambda: ImmutableObjectConflict(path),
    )
    result = ImmutableObject(
        path=path,
        generation=str(blob.generation),
        size=int(blob.size),
        sha256=content_sha256,
        reconciled=reconciled,
    )
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
        blob = immutable_gcs.client().bucket(bucket_name()).get_blob(
            path, generation=int(generation)
        )
        return blob is not None

    return await asyncio.to_thread(_exists)


async def current_generation(path: str) -> str | None:
    """Resolve the sole create-only generation when a receipt write crashed."""

    def _get() -> str | None:
        blob = immutable_gcs.client().bucket(bucket_name()).get_blob(path)
        return str(blob.generation) if blob is not None else None

    return await asyncio.to_thread(_get)


async def download_exact(path: str, generation: str) -> bytes:
    return await immutable_gcs.download_exact(bucket_name(), path, generation)


async def delete_exact(path: str, generation: str) -> bool:
    """Delete one recorded generation. Missing is success for retry recovery."""
    return await immutable_gcs.delete_exact(bucket_name(), path, generation)


async def delete_user_audio(uid: str) -> int:
    """Strict account deletion for every dictation object owned by one user."""
    count = await immutable_gcs.delete_prefix(bucket_name(), user_prefix_for(uid))
    logger.info("dictation.gcs: user audio deleted", {"user_id": uid, "deleted": count})
    return count
