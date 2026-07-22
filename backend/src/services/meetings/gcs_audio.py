"""Cloud Storage - raw meeting audio segments, the short-lived leg only.

Bucket layout:

    meetings/{uid}/{meeting_id}/{seq:04d}.flac

Audio exists in GCS only between upload and synthesis: the worker deletes the
whole prefix immediately after a note is persisted (or the meeting is
excluded), and the bucket carries a 7-day lifecycle delete rule as a backstop
for the failure paths. Nothing here is ever served to a client - unlike
screen_saves there is no signed-URL read path at all.

Account deletion uses a strict user-prefix cleanup before deleting Firestore
or Firebase Auth. Unlike post-synthesis best-effort cleanup, that path raises
on storage failure so deletion can be retried without orphaning raw audio.

Same module shape as services/gcs.py (lazy client singleton, every blocking
call in ``asyncio.to_thread``). Bucket name comes from the MEETINGS_AUDIO_BUCKET
env var (set in deploy.sh) rather than settings.py, keeping this feature's
footprint out of the shared config file.
"""

from __future__ import annotations

import asyncio
import os
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
    return os.getenv("MEETINGS_AUDIO_BUCKET", "juno-2ea45-meeting-audio")


def object_path_for(uid: str, meeting_id: str, seq: int) -> str:
    return f"meetings/{uid}/{meeting_id}/{seq:04d}.flac"


def prefix_for(uid: str, meeting_id: str) -> str:
    return f"meetings/{uid}/{meeting_id}/"


def user_prefix_for(uid: str) -> str:
    return f"meetings/{uid}/"


async def upload_segment(uid: str, meeting_id: str, seq: int, data: bytes) -> str:
    """Upload one FLAC segment. Raises on failure - the handler answers 5xx so
    the desktop's durable queue retries, rather than silently dropping audio
    the user believes was captured."""
    path = object_path_for(uid, meeting_id, seq)

    def _upload() -> None:
        blob = _client().bucket(bucket_name()).blob(path)
        blob.upload_from_string(data, content_type="audio/flac")

    await asyncio.to_thread(_upload)
    logger.info("meetings.gcs: segment uploaded", {
        "user_id": uid, "meeting_id": meeting_id, "seq": seq, "bytes": len(data),
    })
    return path


async def list_segment_paths(uid: str, meeting_id: str) -> list[str]:
    """All uploaded segment object paths for one meeting, sorted by name
    (names embed the zero-padded seq, so name order is seq order). Raises on
    failure - the worker must not synthesize from a partial listing."""
    prefix = prefix_for(uid, meeting_id)

    def _list() -> list[str]:
        blobs = _client().list_blobs(bucket_name(), prefix=prefix)
        return sorted(blob.name for blob in blobs)

    return await asyncio.to_thread(_list)


async def download_segment(path: str) -> bytes:
    """One segment's bytes. Raises on failure (worker retries or fails the run)."""
    def _download() -> bytes:
        return _client().bucket(bucket_name()).blob(path).download_as_bytes()

    return await asyncio.to_thread(_download)


async def delete_meeting_audio(uid: str, meeting_id: str) -> int:
    """Best-effort delete of every audio object for one meeting. Never raises:
    the note is already safe (or the run already failed), and the bucket's
    lifecycle rule mops up anything a transient error leaves behind. Returns
    the number of objects deleted."""
    prefix = prefix_for(uid, meeting_id)

    def _delete() -> int:
        bucket = _client().bucket(bucket_name())
        deleted = 0
        for blob in _client().list_blobs(bucket_name(), prefix=prefix):
            try:
                bucket.blob(blob.name).delete()
                deleted += 1
            except Exception as exc:
                logger.warn("meetings.gcs: segment delete failed", {
                    "path": blob.name, "error": str(exc),
                })
        return deleted

    try:
        count = await asyncio.to_thread(_delete)
        logger.info("meetings.gcs: audio deleted", {
            "user_id": uid, "meeting_id": meeting_id, "deleted": count,
        })
        return count
    except Exception as exc:
        logger.warn("meetings.gcs: audio delete failed", {
            "user_id": uid, "meeting_id": meeting_id, "error": str(exc),
        })
        return 0


async def delete_user_audio(uid: str) -> int:
    """Strict account-deletion cleanup for every raw meeting object owned by a user.

    Unlike post-synthesis cleanup, this raises on any storage failure. The
    account handler keeps Firebase Auth intact so the user can retry instead of
    reporting a completed deletion while raw audio remains in the lifecycle
    backstop window. Partial deletion is safe because object deletes are
    idempotent and a retry lists only what remains.
    """
    prefix = user_prefix_for(uid)

    def _delete() -> int:
        bucket = _client().bucket(bucket_name())
        blobs = list(_client().list_blobs(bucket_name(), prefix=prefix))
        for blob in blobs:
            bucket.blob(blob.name).delete()
        return len(blobs)

    count = await asyncio.to_thread(_delete)
    logger.info("meetings.gcs: user audio deleted", {
        "user_id": uid,
        "deleted": count,
    })
    return count
