"""Screen frames streamed from the desktop client into a live voice session.

The Windows desktop overlay lets the user ARM screen sight (Ctrl+Alt+S or the eye
button); while armed, the client captures the display the cursor is on and sends one
JPEG per user turn over a LiveKit byte stream (topic ``screen_frame``), timed to land
while the user is still talking. This module assembles those streams, keeps ONLY the
newest frame in process memory (never on disk, never in Firestore), and attaches it to
the user's turn as ``ImageContent`` so the vision-capable LLM pipeline can see it.

Arming is entirely client-side: frames either arrive or they don't. A session where the
user never arms screen sight goes through :func:`attach_screen_frame_to_turn` as a pure
no-op, which matters because LiveKit's preemptive generation reuses its speculative
reply only when the hook changed nothing (see below).

Preemptive-generation interplay (the non-obvious part): the AgentSession speculatively
generates a reply from the raw transcript BEFORE ``on_user_turn_completed`` runs, and
keeps it only if the hook left the transcript and chat context untouched. Appending the
frame label string to ``new_message`` changes ``text_content``, which invalidates the
speculative (imageless) reply, so the turn regenerates WITH the image. Without that,
the injected screenshot would be silently ignored on every armed turn.

Every path here is fail-soft: an exception escaping ``on_user_turn_completed`` makes
LiveKit drop the whole turn reply, so nothing in this module may raise.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass

from livekit.agents import llm as lk_llm

from ...lib.logger import logger

# Byte-stream topic the desktop client publishes frames on (single source of truth;
# the Flutter client sends this exact string).
SCREEN_FRAME_TOPIC = "screen_frame"

# A 1280-long-edge JPEG is ~100-300KB; anything past this cap is a bug or abuse,
# dropped loudly rather than buffered.
_MAX_FRAME_BYTES = 2_000_000

# A frame older than this no longer reflects "their screen right now", so it is
# never injected. The client captures per turn, so a fresh frame normally exists.
_FRAME_MAX_AGE_S = 15.0

# When a frame is mid-transfer at turn end, wait briefly for it instead of going
# imageless; past this the reply matters more than the picture.
_INFLIGHT_FRAME_WAIT_S = 0.8

# What an old turn's screenshot collapses into, so exactly one image is ever hot in
# context (token cost) while the transcript still shows one existed.
_STALE_IMAGE_PLACEHOLDER = "[screenshot from an earlier moment removed]"


@dataclass
class ScreenFrame:
    """One assembled JPEG frame plus the metadata the client stamped on the stream."""

    jpeg_bytes: bytes
    attributes: dict[str, str]
    received_at_monotonic: float

    def attribute_int(self, key: str) -> int | None:
        try:
            return int(self.attributes[key])
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def frame_id(self) -> str:
        return self.attributes.get("frame_id", "")

    @property
    def width_px(self) -> int | None:
        return self.attribute_int("jpeg_width_px")

    @property
    def height_px(self) -> int | None:
        return self.attribute_int("jpeg_height_px")

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.received_at_monotonic


class ScreenFrameStore:
    """Latest-frame cache fed by the room's ``screen_frame`` byte-stream handler.

    Registered in voice_agent.py BEFORE ``session.start`` so a frame that lands while
    the pipelines are still building is assembled, not dropped. Only the newest frame
    is kept; a session that never receives one costs nothing.
    """

    def __init__(self, *, session_id: str, user_id: str) -> None:
        self._session_id = session_id
        self._user_id = user_id
        self._latest: ScreenFrame | None = None
        self._inflight_count = 0
        self._frame_landed = asyncio.Event()
        self._assembly_tasks: set[asyncio.Task] = set()
        self._frame_count = 0

    @property
    def has_ever_received_frame(self) -> bool:
        return self._latest is not None

    @property
    def frame_count(self) -> int:
        """How many frames this session ever assembled successfully. Metadata only
        (for the desktop history screen's "screen-sight used Nx" line) — never the
        frame bytes themselves, which this store still only ever keeps one of."""
        return self._frame_count

    def handle_stream(self, reader, participant_identity: str) -> None:
        """Sync callback for ``room.register_byte_stream_handler``; assembles async."""
        task = asyncio.create_task(
            self._assemble_frame(reader, participant_identity),
            name=f"voice-screen-frame-{self._session_id[:8]}",
        )
        self._assembly_tasks.add(task)
        task.add_done_callback(self._assembly_tasks.discard)

    async def _assemble_frame(self, reader, participant_identity: str) -> None:
        self._inflight_count += 1
        self._frame_landed.clear()
        try:
            chunks = bytearray()
            async for chunk in reader:
                chunks.extend(chunk)
                if len(chunks) > _MAX_FRAME_BYTES:
                    logger.warn("VoiceSession: screen frame over size cap, dropped", {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "participant": participant_identity,
                        "bytes_so_far": len(chunks),
                        "cap": _MAX_FRAME_BYTES,
                    })
                    return
            if not chunks:
                logger.warn("VoiceSession: empty screen frame stream, dropped", {
                    "session_id": self._session_id, "user_id": self._user_id,
                })
                return
            attributes = dict(getattr(reader.info, "attributes", None) or {})
            self._latest = ScreenFrame(
                jpeg_bytes=bytes(chunks),
                attributes=attributes,
                received_at_monotonic=time.monotonic(),
            )
            self._frame_count += 1
            logger.info("VoiceSession: screen frame received", {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "bytes": len(chunks),
                "frame_id": self._latest.frame_id,
                "jpeg_px": f"{self._latest.width_px}x{self._latest.height_px}",
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warn("VoiceSession: screen frame assembly failed", {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "error": str(exc),
            })
        finally:
            self._inflight_count -= 1
            self._frame_landed.set()

    async def fresh_frame(self) -> ScreenFrame | None:
        """The newest frame if it still reflects the screen; waits briefly on an
        in-flight transfer so a frame racing the turn boundary isn't missed."""
        if self._inflight_count > 0:
            try:
                await asyncio.wait_for(
                    self._frame_landed.wait(), timeout=_INFLIGHT_FRAME_WAIT_S
                )
            except TimeoutError:
                pass
        frame = self._latest
        if frame is None:
            return None
        if frame.age_seconds > _FRAME_MAX_AGE_S:
            logger.info("VoiceSession: screen frame too stale, not injected", {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "age_s": round(frame.age_seconds, 1),
            })
            return None
        return frame


def _strip_stale_images(turn_ctx: lk_llm.ChatContext) -> int:
    """Collapse earlier turns' screenshots into text placeholders.

    ``turn_ctx`` is a shallow copy sharing message objects with the agent's real
    history, so this in-place mutation also cleans the persistent context: image
    tokens are paid for exactly one turn.
    """
    stripped = 0
    for item in turn_ctx.items:
        if getattr(item, "type", None) != "message":
            continue
        content = getattr(item, "content", None)
        if not isinstance(content, list):
            continue
        for index, part in enumerate(content):
            if isinstance(part, lk_llm.ImageContent):
                content[index] = _STALE_IMAGE_PLACEHOLDER
                stripped += 1
    return stripped


def _frame_label(frame: ScreenFrame) -> str:
    dims = ""
    if frame.width_px and frame.height_px:
        dims = f", {frame.width_px}x{frame.height_px} pixels"
    return (
        "A screenshot of the user's screen accompanies this message "
        f"(the display their cursor is on{dims})."
    )


async def attach_screen_frame_to_turn(
    store: ScreenFrameStore,
    turn_ctx: lk_llm.ChatContext,
    new_message: lk_llm.ChatMessage,
    *,
    session_id: str,
    user_id: str,
) -> ScreenFrame | None:
    """Attach the freshest screen frame to the user's turn; strict no-op when unarmed.

    Called from ``BuddyAgent.on_user_turn_completed``. When no frame has ever arrived
    this returns without touching anything, preserving preemptive generation for
    non-screen-sight sessions. Returns the injected frame (the pointing publisher
    stamps its id into element.point) or None. Never raises (a raised hook drops
    the whole turn reply).
    """
    try:
        if not store.has_ever_received_frame:
            return None

        stripped = _strip_stale_images(turn_ctx)

        frame = await store.fresh_frame()
        if frame is None:
            if stripped:
                logger.info("VoiceSession: stale screenshots stripped, none injected", {
                    "session_id": session_id, "user_id": user_id, "stripped": stripped,
                })
            return None

        data_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(frame.jpeg_bytes).decode("ascii")
        )
        # The label string changes new_message.text_content, which deliberately
        # invalidates the speculative imageless reply (see module docstring).
        new_message.content.append(_frame_label(frame))
        new_message.content.append(
            lk_llm.ImageContent(image=data_url, mime_type="image/jpeg")
        )
        logger.info("VoiceSession: screen frame injected into turn", {
            "session_id": session_id,
            "user_id": user_id,
            "frame_id": frame.frame_id,
            "frame_age_s": round(frame.age_seconds, 1),
            "stripped_stale": stripped,
        })
        return frame
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warn("VoiceSession: screen frame injection failed", {
            "session_id": session_id, "user_id": user_id, "error": str(exc),
        })
        return None
