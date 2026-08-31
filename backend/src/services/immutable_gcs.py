"""Shared mechanics for create-only (immutable) Cloud Storage objects.

Extracted from services/dictation/gcs_audio.py and services/meetings/gcs_audio.py,
which had near-identical copies of the same four mechanisms:

- a lazy per-process ``storage.Client`` singleton (``client()``), also used by
  services/gcs.py so the whole backend shares ONE client;
- ``create_or_reconcile``: an ``if_generation_match=0`` upload where the only
  accepted precondition failure is an existing object whose size, required
  metadata, and (optionally) content type match exactly -- anything else raises
  the caller's conflict exception;
- generation-pinned ``download_exact`` / ``delete_exact``;
- ``delete_prefix``: enumerate a prefix and delete each exact generation.

Feature modules keep their own paths, metadata composition, result dataclasses,
exception types, and log lines; only the storage mechanics live here. Every
blocking call runs in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

_client_singleton: Any = None


def client() -> Any:
    """The process-wide lazy ``storage.Client`` singleton."""
    global _client_singleton
    if _client_singleton is None:
        from google.cloud import storage  # type: ignore

        _client_singleton = storage.Client()
    return _client_singleton


async def create_or_reconcile(
    *,
    bucket_name: str,
    path: str,
    data: bytes,
    content_type: str,
    required_metadata: dict[str, str],
    verify_content_type: bool,
    make_conflict: Callable[[], Exception],
) -> tuple[Any, bool]:
    """Create one immutable object, or reconcile against an identical existing one.

    Uploads with a mandatory generation-zero precondition. On PreconditionFailed
    the live blob is fetched and accepted only when its size, required metadata,
    and (when ``verify_content_type`` is set) content type match exactly;
    a mismatch raises ``make_conflict()``. Returns ``(blob, reconciled)`` where
    the blob is freshly reloaded so callers can snapshot generation and size.
    """

    def _verify(blob: Any) -> None:
        actual = blob.metadata or {}
        mismatch = int(blob.size or -1) != len(data) or any(
            str(actual.get(key, "")) != value for key, value in required_metadata.items()
        )
        if not mismatch and verify_content_type:
            mismatch = str(getattr(blob, "content_type", "")) != content_type
        if mismatch:
            raise make_conflict()

    def _create() -> tuple[Any, bool]:
        from google.api_core.exceptions import PreconditionFailed  # type: ignore

        bucket = client().bucket(bucket_name)
        blob = bucket.blob(path)
        blob.metadata = required_metadata
        try:
            blob.upload_from_string(
                data,
                content_type=content_type,
                if_generation_match=0,
                checksum="auto",
            )
            blob.reload()
            _verify(blob)
            return blob, False
        except PreconditionFailed:
            existing = bucket.get_blob(path)
            if existing is None:
                raise
            _verify(existing)
            return existing, True

    return await asyncio.to_thread(_create)


async def download_exact(bucket_name: str, path: str, generation: str) -> bytes:
    def _download() -> bytes:
        blob = client().bucket(bucket_name).blob(path, generation=int(generation))
        return blob.download_as_bytes(if_generation_match=int(generation))

    return await asyncio.to_thread(_download)


async def delete_exact(bucket_name: str, path: str, generation: str) -> bool:
    """Delete one recorded generation. Missing (already absent) returns False."""

    def _delete() -> bool:
        from google.api_core.exceptions import NotFound  # type: ignore

        blob = client().bucket(bucket_name).blob(path, generation=int(generation))
        try:
            blob.delete(if_generation_match=int(generation))
            return True
        except NotFound:
            return False

    return await asyncio.to_thread(_delete)


async def delete_prefix(bucket_name: str, prefix: str) -> int:
    """Enumerate every object under a prefix and delete each exact generation.

    Raises on any storage failure; partial deletion is safe because deletes are
    idempotent and a retry lists only what remains.
    """

    def _delete() -> int:
        bucket = client().bucket(bucket_name)
        blobs = list(client().list_blobs(bucket_name, prefix=prefix))
        for blob in blobs:
            bucket.blob(blob.name, generation=blob.generation).delete(
                if_generation_match=int(blob.generation),
            )
        return len(blobs)

    return await asyncio.to_thread(_delete)
