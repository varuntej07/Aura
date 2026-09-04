"""Screen frames streamed from the desktop client into a live voice session.

During a standard Windows desktop voice session, the client captures the display the
cursor is on and sends one
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
from collections.abc import Callable
from dataclasses import dataclass

from livekit.agents import llm as lk_llm

from ...lib.logger import logger
from .stream_intake import AssemblyTasks, ConsumedIdRing, read_stream_bounded

# Byte-stream topic the desktop client publishes frames on (single source of truth;
# the Flutter client sends this exact string).
SCREEN_FRAME_TOPIC = "screen_frame"

# A 1280-long-edge JPEG is ~100-300KB; anything past this cap is a bug or abuse,
# dropped loudly rather than buffered.
_MAX_FRAME_BYTES = 2_000_000

# A frame older than this no longer reflects "their screen right now", so it is
# never injected. The client captures per turn, so a fresh frame normally exists.
_FRAME_MAX_AGE_S = 15.0

# Save-path staleness bound, looser than injection on purpose: an explicit
# "save this" may reach back a couple of spoken turns, but never to a screen
# from minutes ago (see latest_for_save).
_SAVE_MAX_AGE_S = 60.0

# Long-edge cap applied HERE rather than trusting the client to have done it.
# Desktop is supposed to send a 1280-long-edge JPEG, but a real session was
# observed shipping 2880x1800, which is ~2.5k vision tokens on every single turn
# and the one part of the prompt that can never be prefix-cached. Downscaling in
# the worker fixes it for every already-installed desktop build at once, with no
# cross-repo contract change, so an old client cannot opt out of the fix.
#
# 1280 is chosen to stay above the detail floor for reading UI labels and button
# text, which is what Guide Mode needs the frame for at all.
_MODEL_FRAME_LONG_EDGE_PX = 1280
_MODEL_FRAME_JPEG_QUALITY = 82

# When a frame is mid-transfer at turn end, wait briefly for it instead of going
# imageless; past this the reply matters more than the picture.
_INFLIGHT_FRAME_WAIT_S = 0.8

# What an old turn's screenshot collapses into, so exactly one image is ever hot in
# context (token cost) while the transcript still shows one existed.
_STALE_IMAGE_PLACEHOLDER = "[screenshot from an earlier moment removed]"

def _downscale_for_model(jpeg_bytes: bytes) -> tuple[bytes, float]:
    """Shrink an oversized frame to the long-edge cap. Returns (bytes, scale applied).

    Pure and synchronous so the caller can push it onto a thread: a 2880x1800
    decode/resize/encode is tens of milliseconds, which is far too long to hold the
    event loop during a live call.

    Fail-soft in both directions. If Pillow cannot read the payload, or the re-encode
    somehow comes out larger than the original, the original bytes pass through at
    scale 1.0. A frame the model can see is always better than no frame, and this
    runs on every turn, so it must never be able to raise.
    """
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(jpeg_bytes)) as image:
            long_edge = max(image.width, image.height)
            if long_edge <= _MODEL_FRAME_LONG_EDGE_PX:
                return jpeg_bytes, 1.0
            scale = _MODEL_FRAME_LONG_EDGE_PX / long_edge
            resized = image.convert("RGB").resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
            buffer = BytesIO()
            resized.save(
                buffer,
                format="JPEG",
                quality=_MODEL_FRAME_JPEG_QUALITY,
                optimize=True,
            )
            shrunk = buffer.getvalue()
        if not shrunk or len(shrunk) >= len(jpeg_bytes):
            return jpeg_bytes, 1.0
        return shrunk, scale
    except Exception:
        return jpeg_bytes, 1.0


@dataclass
class ScreenFrame:
    """One assembled JPEG frame plus the metadata the client stamped on the stream."""

    jpeg_bytes: bytes
    attributes: dict[str, str]
    received_at_monotonic: float
    # How much jpeg_bytes was shrunk from what the client sent. 0.5 means the model
    # sees a half-size image, so a coordinate it reports must be divided by 0.5 to
    # land in the client's original frame geometry. See coordinate_scale in
    # point_tag.publish_element_point: forgetting this silently breaks pointing.
    model_scale: float = 1.0

    def attribute_int(self, key: str) -> int | None:
        try:
            return int(self.attributes[key])
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def frame_id(self) -> str:
        return self.attributes.get("frame_id", "")

    @property
    def turn_context_id(self) -> str:
        """Which speaking turn this frame was captured for.

        Stamped by the desktop the moment the user starts a turn, and shared
        with the structured-context stream, so the backend can tell "the frame
        for this turn" from "a frame still inside the 15s freshness window that
        a previous turn already used".
        """
        return self.attributes.get("turn_context_id", "")

    @property
    def sequence(self) -> int | None:
        return self.attribute_int("frame_seq")

    @property
    def width_px(self) -> int | None:
        """Width of the image the MODEL sees, not what the client captured.

        Every consumer of this (the frame label, the guide kernel, the bounds check
        in guide_task_runtime) reasons about coordinates the model produced, so it
        must be the post-downscale space. The client never reads this back: it maps
        points against its own record for frame_id.
        """
        return self._scaled("jpeg_width_px")

    @property
    def height_px(self) -> int | None:
        return self._scaled("jpeg_height_px")

    def _scaled(self, key: str) -> int | None:
        raw = self.attribute_int(key)
        if raw is None:
            return None
        return max(1, round(raw * self.model_scale))

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.received_at_monotonic

    @property
    def active_process(self) -> str:
        return self.attributes.get("active_process", "")

    @property
    def active_window_id(self) -> str:
        return self.attributes.get("active_window_id", "")

    @property
    def geometry_revision(self) -> int | None:
        return self.attribute_int("geometry_revision")

    @property
    def semantic_metadata(self) -> dict[str, str | int | None]:
        return {
            "guide_session_id": self.attributes.get("guide_session_id", ""),
            "task_id": self.attributes.get("task_id", ""),
            "frame_id": self.frame_id,
            "frame_seq": self.sequence,
            "captured_at_ms": self.attribute_int("captured_at_ms"),
            "captured_monotonic_ms": self.attribute_int("captured_monotonic_ms"),
            "capture_reason": self.attributes.get("capture_reason", ""),
            "change": self.attributes.get("change", ""),
            "active_process": self.active_process,
            "active_window_id": self.active_window_id,
            "active_window_title": self.attributes.get("active_window_title", ""),
            "geometry_revision": self.geometry_revision,
            "frame_hash": self.attributes.get("frame_hash", ""),
            "predecessor_hash": self.attributes.get("predecessor_hash", ""),
            "trace_id": self.attributes.get("trace_id", ""),
            "event_id": self.attributes.get("event_id", ""),
            "parent_event_id": self.attributes.get("parent_event_id", ""),
            "jpeg_width_px": self.width_px,
            "jpeg_height_px": self.height_px,
            "monitor_left_px": self.attribute_int("monitor_left_px"),
            "monitor_top_px": self.attribute_int("monitor_top_px"),
            "monitor_width_px": self.attribute_int("monitor_width_px"),
            "monitor_height_px": self.attribute_int("monitor_height_px"),
        }


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
        self._assembly_tasks = AssemblyTasks()
        self._frame_count = 0
        self._frame_listener: Callable[[ScreenFrame], None] | None = None
        self._consumed_turn_ids = ConsumedIdRing()

    @property
    def has_ever_received_frame(self) -> bool:
        return self._latest is not None

    @property
    def frame_count(self) -> int:
        """How many frames this session ever assembled successfully. Metadata only
        (for the desktop history screen's "screen-sight used Nx" line) — never the
        frame bytes themselves, which this store still only ever keeps one of."""
        return self._frame_count

    def set_frame_listener(self, listener: Callable[[ScreenFrame], None]) -> None:
        """Notify a live feature after a frame has assembled successfully."""
        self._frame_listener = listener

    def handle_stream(self, reader, participant_identity: str) -> None:
        """Sync callback for ``room.register_byte_stream_handler``; assembles async."""
        self._assembly_tasks.spawn(
            self._assemble_frame(reader, participant_identity),
            name=f"voice-screen-frame-{self._session_id[:8]}",
        )

    async def _assemble_frame(self, reader, participant_identity: str) -> None:
        if participant_identity != self._user_id:
            logger.warn(
                "VoiceSession: screen frame participant rejected",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "participant": participant_identity,
                    "outcome": "failed",
                    "reason": "participant_identity_mismatch",
                },
            )
            return
        self._inflight_count += 1
        self._frame_landed.clear()
        attributes = dict(getattr(reader.info, "attributes", None) or {})
        trace_fields = {
            "trace_id": attributes.get("trace_id") or None,
            "event_id": attributes.get("event_id") or None,
            "parent_event_id": attributes.get("parent_event_id") or None,
            "stage": "capture",
        }
        try:
            chunks = await read_stream_bounded(reader, _MAX_FRAME_BYTES)
            if chunks is None:
                logger.warn(
                    "VoiceSession: screen frame over size cap, dropped",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "participant": participant_identity,
                        "cap": _MAX_FRAME_BYTES,
                        "outcome": "failed",
                        "reason": "frame_size_limit_exceeded",
                        **trace_fields,
                    },
                )
                return
            if not chunks:
                logger.warn(
                    "VoiceSession: empty screen frame stream, dropped",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "outcome": "failed",
                        "reason": "empty_frame_stream",
                        **trace_fields,
                    },
                )
                return
            incoming = ScreenFrame(
                jpeg_bytes=bytes(chunks),
                attributes=attributes,
                received_at_monotonic=time.monotonic(),
            )
            latest_sequence = self._latest.sequence if self._latest else None
            if (
                incoming.sequence is not None
                and latest_sequence is not None
                and incoming.sequence < latest_sequence
            ):
                logger.info(
                    "VoiceSession: out-of-order screen frame dropped",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "frame_id": incoming.frame_id,
                        "newest_frame_id": self._latest.frame_id if self._latest else "",
                    },
                )
                return
            # After the ordering check so a frame that loses the race costs no CPU,
            # and off the event loop so the resize cannot stall the live call.
            shrunk_bytes, model_scale = await asyncio.to_thread(
                _downscale_for_model, incoming.jpeg_bytes
            )
            incoming.jpeg_bytes = shrunk_bytes
            incoming.model_scale = model_scale

            self._latest = incoming
            self._frame_count += 1
            logger.info(
                "VoiceSession: screen frame received",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "bytes": len(chunks),
                    "bytes_to_model": len(shrunk_bytes),
                    "model_scale": round(model_scale, 4),
                    "frame_id": self._latest.frame_id,
                    "jpeg_px": f"{self._latest.width_px}x{self._latest.height_px}",
                    "stage": "capture",
                    "outcome": "succeeded",
                    **trace_fields,
                },
            )
            if self._frame_listener is not None:
                try:
                    self._frame_listener(self._latest)
                except Exception as exc:
                    logger.warn(
                        "VoiceSession: screen frame listener failed",
                        {
                            "session_id": self._session_id,
                            "user_id": self._user_id,
                            "error": str(exc),
                            "outcome": "failed",
                            "reason": "frame_listener_failed",
                            **trace_fields,
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warn(
                "VoiceSession: screen frame assembly failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "error": str(exc),
                    "outcome": "failed",
                    "reason": "frame_assembly_failed",
                    "error_type": type(exc).__name__,
                    **trace_fields,
                },
            )
        finally:
            self._inflight_count -= 1
            self._frame_landed.set()

    async def fresh_frame(
        self, *, current_turn_context_id: str | None = None
    ) -> ScreenFrame | None:
        """The newest frame if it still reflects the screen; waits briefly on an
        in-flight transfer so a frame racing the turn boundary isn't missed.

        ``current_turn_context_id`` exempts the frame this exact turn already
        consumed (e.g. via the general vision attach in
        ``on_user_turn_completed``) from the staleness check below: a mid-turn
        tool call runs AFTER that attach has already marked the frame
        consumed, so without this exemption a tool would spuriously see its
        own turn's just-shown frame as unavailable. Only an exact id match is
        exempt, so a genuinely older turn's frame is still rejected.
        """
        await self._wait_for_inflight_frame()
        frame = self._latest
        if frame is None:
            return None
        if (
            frame.turn_context_id
            and frame.turn_context_id in self._consumed_turn_ids
            and frame.turn_context_id != current_turn_context_id
        ):
            # A previous turn already showed the model this exact frame. Without
            # this the freshness window alone would re-attach it: a structured
            # -context turn arriving within 15s of a pixel turn would silently
            # answer against the older screenshot.
            return None
        if frame.age_seconds > _FRAME_MAX_AGE_S:
            logger.info(
                "VoiceSession: screen frame too stale, not injected",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "age_s": round(frame.age_seconds, 1),
                },
            )
            return None
        return frame

    async def latest_for_save(self) -> ScreenFrame | None:
        """Return the latest session frame for an explicit user-authorized save.

        LLM freshness and save availability are intentionally different. A frame
        already attached to a prior model turn must not be injected again, but its
        bytes remain the last screen Buddy actually saw and may be persisted when
        the user explicitly asks. The cache is memory-only and is cleared at
        session shutdown.

        Save availability is still AGE-bounded, more loosely than injection:
        "add this to Notion" minutes after the relevant screen was replaced
        must not silently persist the older screen (the only path on macOS,
        where pixels are the sole capture). Mirrors the structured store's
        latest_for_save bound.
        """
        await self._wait_for_inflight_frame()
        frame = self._latest
        if frame is None:
            return None
        if frame.age_seconds > _SAVE_MAX_AGE_S:
            logger.info(
                "VoiceSession: screen frame too stale for save",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "age_s": round(frame.age_seconds, 1),
                },
            )
            return None
        return frame

    async def _wait_for_inflight_frame(self) -> None:
        if self._inflight_count <= 0:
            return
        try:
            await asyncio.wait_for(
                self._frame_landed.wait(), timeout=_INFLIGHT_FRAME_WAIT_S
            )
        except TimeoutError:
            pass

    def close(self) -> None:
        """Release session-only pixels and stop outstanding assembly work."""
        self._latest = None
        self._frame_listener = None
        self._consumed_turn_ids.clear()
        self._assembly_tasks.cancel_all()

    def mark_turn_consumed(self, turn_context_id: str) -> None:
        """Record that a finalized turn already carries this turn's frame."""
        self._consumed_turn_ids.remember(turn_context_id)


def strip_stale_images(turn_ctx: lk_llm.ChatContext) -> int:
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
    """Introduce the attached frame, and state the POINT coordinate space.

    The dimensions are here for exactly one reason: `[POINT:x,y:label]` is specified
    in prompts.py as "integer pixels from the frame's top-left" without ever naming a
    range, so this is the only place the model learns the bounds. They are phrased as
    a coordinate space rather than a size ("1280x800 pixels") because the size framing
    was being read back to users as a complaint about the image being too small.
    """
    bounds = ""
    if frame.width_px and frame.height_px:
        bounds = f" Pointing coordinates in it run from 0,0 to {frame.width_px - 1},{frame.height_px - 1}."
    return (
        "A screenshot of the user's screen accompanies this message "
        f"(the display their cursor is on).{bounds}"
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

        stripped = strip_stale_images(turn_ctx)

        frame = await store.fresh_frame()
        if frame is None:
            if stripped:
                logger.info(
                    "VoiceSession: stale screenshots stripped, none injected",
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "stripped": stripped,
                    },
                )
            return None

        data_url = "data:image/jpeg;base64," + base64.b64encode(frame.jpeg_bytes).decode("ascii")
        # The label string changes new_message.text_content, which deliberately
        # invalidates the speculative imageless reply (see module docstring).
        new_message.content.append(_frame_label(frame))
        # inference_detail is pinned rather than left at LiveKit's "auto", which is
        # forwarded verbatim to OpenAI as image_url.detail and lets an undocumented
        # provider-side heuristic decide fidelity on a live path. Read only by the
        # OpenAI legs; the Anthropic and Gemini fallbacks in pipelines.py ignore it.
        new_message.content.append(
            lk_llm.ImageContent(
                image=data_url, mime_type="image/jpeg", inference_detail="high"
            )
        )
        logger.info(
            "VoiceSession: screen frame injected into turn",
            {
                "session_id": session_id,
                "user_id": user_id,
                "frame_id": frame.frame_id,
                "frame_age_s": round(frame.age_seconds, 1),
                "stripped_stale": stripped,
            },
        )
        return frame
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warn(
            "VoiceSession: screen frame injection failed",
            {
                "session_id": session_id,
                "user_id": user_id,
                "error": str(exc),
                "stage": "capture",
                "outcome": "failed",
                "reason": "frame_injection_failed",
                "error_type": type(exc).__name__,
            },
        )
        return None
