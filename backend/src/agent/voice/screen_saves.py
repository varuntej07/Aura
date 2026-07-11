"""
``save_screen_item`` — the local, in-process voice tool for persisting one
screen-sight frame the user explicitly asked Buddy to remember ("save these
shoes for later").

Why this can't be an MCP tool like every other one (``track_topic``,
``store_memory``, ``send_email``, ...): those all execute over HTTP in the
main backend process (``handlers/mcp.py`` -> ``tool_executor.py``), which
never sees the frame bytes — :class:`ScreenFrameStore` lives only in THIS
process's memory, scoped to one LiveKit session (see ``screen_frames.py``).
This module is called directly from a local ``@function_tool`` method on
``BuddyAgent`` instead (see ``buddy_agent.py``), the same process that
already holds the frame and already talks to Firestore directly
(``voice/fetchers.py``).

Persistence only ever happens on this explicit, user-asked-for path — every
OTHER armed turn's frame still follows the existing "never on disk, never in
Firestore" path in ``screen_frames.py``, completely untouched by this module.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from livekit.agents import get_job_context

from ...lib.logger import logger
from ...services import gcs
from ...services.screen_saves import fields as F
from ...services.screen_saves import store as screen_save_store
from ...services.screen_saves.collections import resolve_collection_name
from .screen_frames import ScreenFrameStore


@dataclass
class SaveScreenItemResult:
    """What the calling tool method hands back to the model as its tool
    result, plus what BuddyAgent needs to publish the desktop confirmation."""

    spoken_confirmation: str
    item_id: str | None
    collection_name: str | None


async def save_screen_item(
    *,
    uid: str,
    session_id: str,
    screen_frames: ScreenFrameStore | None,
    title: str,
    collection_name: str,
    description: str = "",
    note: str = "",
    source_url: str | None = None,
) -> SaveScreenItemResult:
    """Persist one screen save and publish a confirmation over the room.

    Never raises into the calling tool — every failure degrades to a spoken
    message the model can pass along, since a raised tool call surfaces as a
    generic error mid-voice-turn rather than something Buddy can talk through.
    """
    title = (title or "").strip()
    if not title:
        return SaveScreenItemResult(
            spoken_confirmation="I didn't catch what to save — what should I call it?",
            item_id=None, collection_name=None,
        )

    frame = None
    if screen_frames is not None:
        try:
            frame = await screen_frames.fresh_frame()
        except Exception as exc:
            logger.warn("screen_saves: fresh_frame failed, saving without an image", {
                "user_id": uid, "session_id": session_id, "error": str(exc),
            })

    resolved = await resolve_collection_name(uid, collection_name)
    item_id = F.new_item_id()

    # image_path is a deterministic path (uid + item_id), not "proof the
    # upload succeeded" — the upload and the Firestore write below run
    # concurrently, each depending only on the resolved collection name, so
    # neither can know the other's outcome ahead of time. If the upload leg
    # fails, the doc is patched to drop the dangling reference afterward.
    image_path = gcs.object_path_for(uid, item_id) if frame is not None else None

    write_coro = screen_save_store.create_item(
        uid, item_id,
        title=title,
        collection_name=resolved.display_name,
        description=description,
        note=note,
        source_url=source_url,
        image_path=image_path,
        session_id=session_id,
        source_frame_id=frame.frame_id if frame else None,
    )

    if frame is not None:
        upload_result, write_result = await asyncio.gather(
            gcs.upload_screen_save(uid, item_id, frame.jpeg_bytes),
            write_coro,
            return_exceptions=True,
        )
        if isinstance(upload_result, Exception):
            logger.warn("screen_saves: image upload failed, saved as text-only", {
                "user_id": uid, "item_id": item_id, "error": str(upload_result),
            })
            if not isinstance(write_result, Exception):
                await screen_save_store.clear_image_path(uid, item_id)
        if isinstance(write_result, Exception):
            logger.error("screen_saves: item write failed", {
                "user_id": uid, "item_id": item_id, "error": str(write_result),
            })
            return SaveScreenItemResult(
                spoken_confirmation="Something went wrong saving that — try again?",
                item_id=None, collection_name=None,
            )
    else:
        try:
            await write_coro
        except Exception as exc:
            logger.error("screen_saves: item write failed", {
                "user_id": uid, "item_id": item_id, "error": str(exc),
            })
            return SaveScreenItemResult(
                spoken_confirmation="Something went wrong saving that — try again?",
                item_id=None, collection_name=None,
            )

    await _publish_screen_save_created(
        item_id=item_id, title=title, collection_name=resolved.display_name,
        session_id=session_id, user_id=uid,
    )

    logger.info("screen_saves: item saved", {
        "user_id": uid, "session_id": session_id, "item_id": item_id,
        "collection_name": resolved.display_name, "had_image": frame is not None,
        "collection_is_new": resolved.is_new,
    })
    return SaveScreenItemResult(
        spoken_confirmation=f"Saved to {resolved.display_name}.",
        item_id=item_id, collection_name=resolved.display_name,
    )


async def _publish_screen_save_created(
    *, item_id: str, title: str, collection_name: str, session_id: str, user_id: str,
) -> None:
    """Push a confirmation down the data channel for the desktop overlay's
    toast. Payload shape: {type: 'screen_save.created', payload: {item_id,
    collection_name, title}}. Fail-soft, exactly like point_tag.py's
    publish_element_point — a lost event costs a toast, never the reply."""
    try:
        room = get_job_context().room
        payload = json.dumps({
            "type": "screen_save.created",
            "payload": {"item_id": item_id, "collection_name": collection_name, "title": title},
        }).encode("utf-8")
        await room.local_participant.publish_data(payload, reliable=True)
        logger.info("screen_saves: screen_save.created published", {
            "session_id": session_id, "user_id": user_id, "item_id": item_id,
        })
    except Exception as exc:
        logger.warn("screen_saves: screen_save.created publish failed", {
            "session_id": session_id, "user_id": user_id, "error": str(exc),
        })
