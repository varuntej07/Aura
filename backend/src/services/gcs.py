"""
Cloud Storage — screen-save images.

This is the first feature in this backend that persists a blob (every other
store is Firestore-only), so this module is genuinely new infrastructure, not
a reuse of an existing pattern. Bucket layout:

    screen_saves/{uid}/{item_id}.jpg

Read access is only ever through short-lived v4 signed URLs minted per API
response (``signed_url_for``) — the bucket itself is never public.

Deploy prerequisite, not a code concern: v4 signed URL generation needs
Application Default Credentials that can sign a blob. On Cloud Run this means
the runtime service account needs ``roles/iam.serviceAccountTokenCreator``
granted to ITSELF (so the client library can self-sign via the IAM Credentials
API) — a bare ADC token from the metadata server has no private key to sign
with directly. Verify this grant during deploy; a missing grant fails loudly
at the first ``signed_url_for`` call, not at import time.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..config.settings import settings
from ..lib.logger import logger

_client_singleton: Any = None


def _client() -> Any:
    global _client_singleton
    if _client_singleton is None:
        from google.cloud import storage  # type: ignore
        _client_singleton = storage.Client()
    return _client_singleton


def object_path_for(uid: str, item_id: str) -> str:
    """The deterministic object path for one item — pure, no I/O. Callers use
    this to know the future path BEFORE the upload completes, so the upload
    and the Firestore write that references it can run concurrently."""
    return f"screen_saves/{uid}/{item_id}.jpg"


async def upload_screen_save(uid: str, item_id: str, jpeg_bytes: bytes) -> str:
    """Upload one screen-save JPEG. Returns the GCS object path (not a URL) to
    store on the Firestore item doc. Raises on failure — callers that need
    fail-open behavior (a save should still succeed without an image) must
    wrap this themselves, since silently dropping the image is a caller-level
    product decision, not this module's to make."""
    path = object_path_for(uid, item_id)

    def _upload() -> None:
        bucket = _client().bucket(settings.SCREEN_SAVES_BUCKET)
        blob = bucket.blob(path)
        blob.upload_from_string(jpeg_bytes, content_type="image/jpeg")

    await asyncio.to_thread(_upload)
    logger.info("gcs: screen save uploaded", {
        "user_id": uid, "item_id": item_id, "path": path, "bytes": len(jpeg_bytes),
    })
    return path


async def delete_screen_save(path: str) -> bool:
    """Best-effort delete of one screen-save object. Never raises — a failed
    GCS delete must not block the Firestore item delete that owns it."""
    if not path:
        return False

    def _delete() -> None:
        _client().bucket(settings.SCREEN_SAVES_BUCKET).blob(path).delete()

    try:
        await asyncio.to_thread(_delete)
        return True
    except Exception as exc:
        logger.warn("gcs: screen save delete failed", {"path": path, "error": str(exc)})
        return False


async def signed_url_for(path: str, *, ttl_seconds: int | None = None) -> str | None:
    """Short-lived v4 signed GET URL for one object. None (not a raise) on
    failure, so one bad signing call degrades to a card with no image instead
    of failing the whole /screen-saves list."""
    if not path:
        return None
    ttl_seconds = ttl_seconds or settings.SCREEN_SAVES_SIGNED_URL_TTL_S

    def _sign() -> str:
        from datetime import timedelta
        blob = _client().bucket(settings.SCREEN_SAVES_BUCKET).blob(path)
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=ttl_seconds),
            method="GET",
        )

    try:
        return await asyncio.to_thread(_sign)
    except Exception as exc:
        logger.warn("gcs: signed url generation failed", {"path": path, "error": str(exc)})
        return None
