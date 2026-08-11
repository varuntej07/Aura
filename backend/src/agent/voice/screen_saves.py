"""Strict persistence for one explicitly requested desktop screen capture.

This is an internal worker operation, not an LLM tool. The caller supplies the
exact in-memory frame selected from the authenticated LiveKit session. A save
only succeeds after both the JPEG upload and Firestore item write succeed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from io import BytesIO

from livekit.agents import get_job_context

from ...lib.logger import logger
from ...services import gcs
from ...services.screen_saves import fields as F
from ...services.screen_saves import store as screen_save_store
from .screen_frames import ScreenFrame

_SCREENSHOT_COLLECTION = "Screenshots"


@dataclass(frozen=True, slots=True)
class SaveScreenItemResult:
    """Persistence outcome and the short confirmation Buddy may speak verbatim."""

    spoken_confirmation: str
    item_id: str | None
    collection_name: str | None
    image_path: str | None = None
    frame_id: str = ""
    already_saved: bool = False

    @property
    def succeeded(self) -> bool:
        return bool(self.item_id and self.image_path)


def capture_item_id(
    *, uid: str, session_id: str, finalized_message_id: str, frame_id: str
) -> str:
    """Stable id for retries of the same authorized utterance and frame."""
    material = "\x1f".join((uid, session_id, finalized_message_id, frame_id))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


async def save_screen_capture(
    *,
    uid: str,
    session_id: str,
    finalized_message_id: str,
    frame: ScreenFrame,
) -> SaveScreenItemResult:
    """Persist ``frame`` atomically from the user's point of view.

    The upload happens first. Firestore is only written with a real image path,
    and a failed document write triggers best-effort object cleanup. A stable
    item id makes retries idempotent across worker callbacks.
    """
    if not uid or not session_id or not finalized_message_id:
        return _failure(frame.frame_id)
    if not frame.jpeg_bytes:
        logger.warn(
            "screen_saves: capture rejected because frame bytes are empty",
            {"user_id": uid, "session_id": session_id, "frame_id": frame.frame_id},
        )
        return _failure(frame.frame_id)
    if not await asyncio.to_thread(_is_valid_jpeg, frame.jpeg_bytes):
        logger.warn(
            "screen_saves: capture rejected because frame is not a valid JPEG",
            {"user_id": uid, "session_id": session_id, "frame_id": frame.frame_id},
        )
        return _failure(frame.frame_id)

    item_id = capture_item_id(
        uid=uid,
        session_id=session_id,
        finalized_message_id=finalized_message_id,
        frame_id=frame.frame_id,
    )
    image_path = gcs.object_path_for(uid, item_id)
    try:
        existing = await screen_save_store.get_item_strict(uid, item_id)
    except Exception as exc:
        logger.error(
            "screen_saves: idempotency read failed",
            {
                "user_id": uid,
                "session_id": session_id,
                "item_id": item_id,
                "frame_id": frame.frame_id,
                "error": str(exc),
            },
        )
        return _failure(frame.frame_id)
    if existing and existing.get(F.IMAGE_PATH) == image_path:
        return SaveScreenItemResult(
            spoken_confirmation="Already saved it.",
            item_id=item_id,
            collection_name=str(
                existing.get(F.COLLECTION_NAME) or _SCREENSHOT_COLLECTION
            ),
            image_path=image_path,
            frame_id=frame.frame_id,
            already_saved=True,
        )

    title = (frame.attributes.get("active_window_title") or "Screen capture").strip()
    try:
        await gcs.upload_screen_save(uid, item_id, frame.jpeg_bytes)
    except Exception as exc:
        logger.error(
            "screen_saves: JPEG upload failed",
            {
                "user_id": uid,
                "session_id": session_id,
                "item_id": item_id,
                "frame_id": frame.frame_id,
                "error": str(exc),
            },
        )
        return _failure(frame.frame_id)

    try:
        await screen_save_store.create_item(
            uid,
            item_id,
            title=title,
            collection_name=_SCREENSHOT_COLLECTION,
            image_path=image_path,
            session_id=session_id,
            source_frame_id=frame.frame_id or None,
        )
    except Exception as exc:
        await gcs.delete_screen_save(image_path)
        logger.error(
            "screen_saves: item write failed after JPEG upload",
            {
                "user_id": uid,
                "session_id": session_id,
                "item_id": item_id,
                "frame_id": frame.frame_id,
                "error": str(exc),
            },
        )
        return _failure(frame.frame_id)

    await _publish_screen_save_created(
        item_id=item_id,
        title=title,
        collection_name=_SCREENSHOT_COLLECTION,
        session_id=session_id,
        user_id=uid,
    )
    logger.info(
        "screen_saves: capture persisted",
        {
            "user_id": uid,
            "session_id": session_id,
            "item_id": item_id,
            "frame_id": frame.frame_id,
            "collection_name": _SCREENSHOT_COLLECTION,
            "had_image": True,
        },
    )
    return SaveScreenItemResult(
        spoken_confirmation="Saved it.",
        item_id=item_id,
        collection_name=_SCREENSHOT_COLLECTION,
        image_path=image_path,
        frame_id=frame.frame_id,
    )


def _failure(frame_id: str) -> SaveScreenItemResult:
    return SaveScreenItemResult(
        spoken_confirmation="Something went wrong saving that - try again?",
        item_id=None,
        collection_name=None,
        frame_id=frame_id,
    )


def _is_valid_jpeg(payload: bytes) -> bool:
    try:
        from PIL import Image

        with Image.open(BytesIO(payload)) as image:
            if image.format != "JPEG":
                return False
            image.verify()
        return True
    except Exception:
        return False


async def _publish_screen_save_created(
    *, item_id: str, title: str, collection_name: str, session_id: str, user_id: str,
) -> None:
    """Publish the desktop toast after durable persistence; fail soft on toast loss."""
    try:
        room = get_job_context().room
        payload = json.dumps(
            {
                "type": "screen_save.created",
                "payload": {
                    "item_id": item_id,
                    "collection_name": collection_name,
                    "title": title,
                },
            }
        ).encode("utf-8")
        await room.local_participant.publish_data(payload, reliable=True)
        logger.info(
            "screen_saves: screen_save.created published",
            {"session_id": session_id, "user_id": user_id, "item_id": item_id},
        )
    except Exception as exc:
        logger.warn(
            "screen_saves: screen_save.created publish failed",
            {"session_id": session_id, "user_id": user_id, "error": str(exc)},
        )
