"""Natural, bounded screen guidance for an explicitly armed desktop session.

The desktop streams a fresh JPEG roughly every two seconds while Guide Mode is
armed, stamping each frame ``change:"1"`` when its own change-filter classified a
real visible change and ``change:"0"`` for a forced static-screen refresh. This
module does two things with that stream:

* Acks every received frame immediately (a ``guide.step`` "Screen checked."
  message) so the desktop's per-frame handshake never stalls and the next frame
  can flow. Acking is decoupled from replying.
* On a ``change:"1"`` frame it fires ONE terse proactive nudge ("Now click Save,
  top right") at a quiet turn boundary, debounced so it never stacks on a spoken
  reply or on a burst of rapid changes.

Spoken user questions are NOT answered here anymore: ``BuddyAgent.llm_node``
answers them with the same terse guide brain (see ``guide_prompt`` and
``buddy_agent``) when Guide Mode is active, so there is exactly one reply per
spoken turn. This coordinator only handles acking, proactive change nudges, and
usage rollup.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Protocol

from livekit.agents import AgentSession
from livekit.agents import llm as lk_llm

from ...lib.logger import logger
from ...services.guide_usage_store import record_guide_usage
from .guide_prompt import GUIDE_INSTRUCTIONS
from .screen_frames import ScreenFrame, strip_stale_images

GUIDE_MODE_TYPE = "guide.mode"
GUIDE_STEP_TYPE = "guide.step"

_GUIDE_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
_GUIDE_FRAME_RE = re.compile(r"^([0-9a-f]{32}):(\d+)$")
_LISTENING_POLL_INTERVAL_S = 0.25
_LISTENING_MAX_WAIT_S = 15.0
# A frame older than this no longer reflects "their screen right now", so it is
# never used to ground a proactive nudge.
_GUIDE_FRAME_MAX_AGE_S = 15.0
# Never fire two proactive nudges closer than this, however fast the screen churns.
_PROACTIVE_MIN_INTERVAL_S = 4.0
# Stay silent this long after the user actually spoke: the spoken turn already
# answered them, and their own action is what usually changed the screen.
_PROACTIVE_AFTER_TURN_QUIET_S = 2.0


class GuideBuddy(Protocol):
    @property
    def chat_ctx(self) -> lk_llm.ChatContext: ...

    async def update_chat_ctx(self, chat_ctx: lk_llm.ChatContext) -> None: ...

    def set_guide_frame(self, frame_id: str) -> None: ...

    async def apply_guide_persona(self, active: bool) -> None: ...


class GuideCoordinator:
    """Ack frames fast, fire terse change-driven nudges, and roll up usage."""

    def __init__(
        self,
        *,
        session: AgentSession,
        buddy: GuideBuddy,
        room,
        session_id: str,
        user_id: str,
    ) -> None:
        self._session = session
        self._buddy = buddy
        self._room = room
        self._session_id = session_id
        self._user_id = user_id
        self._active = False
        self._guide_session_id = ""
        self._generation = -1
        self._latest_frame: ScreenFrame | None = None
        self._pending_nudge = False
        self._inflight_frame_id = ""
        self._last_acked_frame_id = ""
        self._step_index = 0
        self._wake = asyncio.Event()
        self._last_proactive_at = 0.0
        self._last_user_turn_at = 0.0
        self._task: asyncio.Task | None = None
        self._closed = False
        # Per-guide-session usage the worker alone can see (model/TTFT/tools/last
        # turn). The recorder forwards every turn's metrics/tools/transcript here;
        # note_* ignore them unless a guide session is active, so only the armed
        # window is measured. Flushed to the user's rollup on disarm or close.
        self._ttft_sum_ms = 0
        self._ttft_count = 0
        self._tools_used: set[str] = set()
        self._model = ""
        self._provider = ""
        self._last_user_turn = ""

    def is_active(self) -> bool:
        return self._active

    def current_reply_source(self) -> str:
        return "guide_turn" if self._active else "normal_turn"

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(
                self._run(), name=f"voice-guide-{self._session_id[:8]}"
            )

    async def close(self) -> None:
        self._closed = True
        # A session that ends while Guide Mode is still armed (the user quit
        # without disarming) is flushed here; the desktop's own report is
        # unreliable on a hard quit, so the worker is the durable end-of-session
        # writer. A prior disarm already flushed and cleared the id, so this is a
        # no-op in the normal path.
        if self._active and self._guide_session_id:
            try:
                await self._flush_usage(self._guide_session_id)
            except Exception:
                pass
        self._active = False
        self._wake.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def apply_control(self, message: dict, participant_identity: str) -> bool:
        """Apply an authenticated, monotonic ``guide.mode`` control message."""
        if participant_identity != self._user_id:
            logger.warn(
                "VoiceSession: rejected Guide Mode control from another participant",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "participant": participant_identity,
                },
            )
            return False
        active = message.get("active")
        generation = message.get("generation")
        guide_session_id = message.get("guide_session_id")
        if (
            not isinstance(active, bool)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
        ):
            return False
        if active and (
            not isinstance(guide_session_id, str)
            or _GUIDE_SESSION_RE.fullmatch(guide_session_id) is None
        ):
            return False
        if generation <= self._generation:
            return False

        previous_guide_session_id = self._guide_session_id
        self._generation = generation
        self._active = active
        self._guide_session_id = guide_session_id if active else ""
        self._step_index = 0
        self._pending_nudge = False
        self._inflight_frame_id = ""
        self._last_acked_frame_id = ""
        # Swap the whole agent to the guide skill (no tools) while armed, and back to
        # the companion persona on disarm. Async, so schedule it; it takes effect
        # before the next generation.
        asyncio.create_task(
            self._buddy.apply_guide_persona(active),
            name=f"guide-persona-{self._session_id[:8]}",
        )
        if active:
            self._last_proactive_at = 0.0
            self._reset_usage()
        if not active:
            self._latest_frame = None
            self._wake.clear()
            if previous_guide_session_id:
                asyncio.create_task(
                    self._flush_usage(previous_guide_session_id),
                    name=f"guide-usage-{self._session_id[:8]}",
                )
        logger.info(
            "VoiceSession: Guide Mode changed",
            {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "active": active,
                "generation": generation,
                "guide_session_id": self._guide_session_id,
            },
        )
        return True

    def submit_frame(self, frame: ScreenFrame) -> None:
        """Ack every accepted frame; a ``change:"1"`` frame also wakes a nudge."""
        if frame.attributes.get("mode") != "guide":
            self._log_frame_drop(frame, "wrong_mode")
            return
        match = _GUIDE_FRAME_RE.fullmatch(frame.frame_id)
        if match is None:
            self._log_frame_drop(frame, "invalid_frame_id")
            return
        if not self._active:
            self._log_frame_drop(frame, "inactive_session")
            return
        if match.group(1) != self._guide_session_id:
            self._log_frame_drop(frame, "wrong_session")
            return
        self._latest_frame = frame
        if frame.frame_id == self._last_acked_frame_id:
            # A re-delivery of a frame we already acked (desktop response retry);
            # never double-ack or re-nudge it.
            self._log_frame_drop(frame, "already_acked")
            return
        # Ack fast and decoupled from replying so the desktop handshake never stalls.
        asyncio.create_task(
            self._ack_frame(frame), name=f"guide-ack-{self._session_id[:8]}"
        )
        if frame.attributes.get("change") == "1":
            self._pending_nudge = True
            self._wake.set()

    def _frame_matches_active_session(self, frame: ScreenFrame | None) -> bool:
        if frame is None or not self._active:
            return False
        match = _GUIDE_FRAME_RE.fullmatch(frame.frame_id)
        return bool(
            frame.attributes.get("mode") == "guide"
            and match is not None
            and match.group(1) == self._guide_session_id
        )

    def _reset_usage(self) -> None:
        self._ttft_sum_ms = 0
        self._ttft_count = 0
        self._tools_used = set()
        self._model = ""
        self._provider = ""
        self._last_user_turn = ""

    def note_turn_metrics(self, role: str, metrics: dict) -> None:
        """Fold one assistant turn's LLM TTFT + model into the active guide window."""
        if not self._active or role != "assistant" or not isinstance(metrics, dict):
            return
        ttft = metrics.get("llm_node_ttft")
        if isinstance(ttft, (int, float)):
            self._ttft_sum_ms += int(ttft * 1000)
            self._ttft_count += 1
        llm_meta = metrics.get("llm_metadata") or {}
        model = llm_meta.get("model_name")
        provider = llm_meta.get("model_provider")
        if isinstance(model, str) and model:
            self._model = model
        if isinstance(provider, str) and provider:
            self._provider = provider

    def note_tool(self, name: str) -> None:
        if self._active and name:
            self._tools_used.add(name)

    def note_user_turn(self, text: str) -> None:
        """Record the spoken turn for usage + the post-turn nudge quiet window.

        The spoken turn is answered by the guide brain on the normal path, so this
        no longer triggers a reply here; it only marks that the user just spoke.
        """
        if self._active and isinstance(text, str) and text.strip():
            self._last_user_turn = text.strip()[:500]
            self._last_user_turn_at = time.monotonic()

    def _log_frame_drop(self, frame: ScreenFrame, reason: str) -> None:
        logger.info("VoiceSession: Guide Mode frame dropped", {
            "session_id": self._session_id,
            "user_id": self._user_id,
            "frame_id": frame.frame_id,
            "reason": reason,
        })

    async def _flush_usage(self, guide_session_id: str) -> None:
        """Merge the worker-only fields for one guide session into the user rollup.

        No additive counters (the desktop owns those, to avoid double counting);
        this contributes only the ``guide_last_*`` fields the client cannot see.
        record_guide_usage is fail-soft, so this never raises into session teardown.
        """
        if not guide_session_id:
            return
        avg_ttft_ms = int(self._ttft_sum_ms / self._ttft_count) if self._ttft_count else None
        await record_guide_usage(
            self._user_id,
            guide_session_id=guide_session_id,
            ended_at_ms=int(time.time() * 1000),
            snapshot_fields={
                "guide_last_voice_session_id": self._session_id,
                "guide_last_model": self._model or None,
                "guide_last_provider": self._provider or None,
                "guide_last_avg_ttft_ms": avg_ttft_ms,
                "guide_last_tools_used": sorted(self._tools_used),
                "guide_last_user_turn": self._last_user_turn or None,
                "guide_last_frames_processed": self._step_index,
            },
            increments={},
        )

    async def _wait_for_turn_boundary(self) -> None:
        waited = 0.0
        while waited < _LISTENING_MAX_WAIT_S:
            agent_listening = str(getattr(self._session, "agent_state", "")) == "listening"
            user_state = str(getattr(self._session, "user_state", ""))
            if agent_listening and user_state != "speaking":
                return
            await asyncio.sleep(_LISTENING_POLL_INTERVAL_S)
            waited += _LISTENING_POLL_INTERVAL_S

    async def _run(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            if not self._active or not self._pending_nudge:
                continue
            self._pending_nudge = False

            now = time.monotonic()
            if now - self._last_proactive_at < _PROACTIVE_MIN_INTERVAL_S:
                continue
            if now - self._last_user_turn_at < _PROACTIVE_AFTER_TURN_QUIET_S:
                continue

            # Ground the nudge on the freshest frame, not the one that triggered the
            # wake - the screen may have advanced again since.
            frame = self._latest_frame
            if not self._frame_matches_active_session(frame):
                continue
            assert frame is not None
            if frame.age_seconds > _GUIDE_FRAME_MAX_AGE_S:
                self._log_frame_drop(frame, "stale")
                continue
            if frame.frame_id == self._inflight_frame_id:
                continue

            generation = self._generation
            guide_session_id = self._guide_session_id
            self._inflight_frame_id = frame.frame_id
            try:
                await self._wait_for_turn_boundary()
                if (
                    not self._active
                    or generation != self._generation
                    or guide_session_id != self._guide_session_id
                ):
                    continue
                # Re-take the freshest frame after the (possibly long) boundary wait.
                frame = self._latest_frame
                if not self._frame_matches_active_session(frame):
                    continue
                assert frame is not None
                if frame.age_seconds > _GUIDE_FRAME_MAX_AGE_S:
                    self._log_frame_drop(frame, "stale_at_generation")
                    continue
                self._inflight_frame_id = frame.frame_id
                await self._prepare_context(frame)
                message = lk_llm.ChatMessage(
                    role="user",
                    content=[
                        "The currently visible screen changed while Guide Mode is active.",
                        lk_llm.ImageContent(
                            image=(
                                "data:image/jpeg;base64,"
                                + base64.b64encode(frame.jpeg_bytes).decode("ascii")
                            ),
                            mime_type="image/jpeg",
                        ),
                    ],
                )
                speech = self._session.generate_reply(
                    user_input=message,
                    instructions=GUIDE_INSTRUCTIONS,
                    tools=[],
                )
                await speech
                self._last_proactive_at = time.monotonic()
                logger.info(
                    "VoiceSession: Guide Mode nudge delivered",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "frame_id": frame.frame_id,
                        "source": "guide_turn",
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Stay silent on failure - a proactive nudge must never turn into a
                # spoken apology like "I can't see the current screen".
                logger.warn(
                    "VoiceSession: Guide Mode nudge failed",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "frame_id": frame.frame_id,
                        "error": str(exc),
                    },
                )
            finally:
                self._inflight_frame_id = ""

    async def _prepare_context(self, frame: ScreenFrame) -> None:
        context = self._buddy.chat_ctx.copy()
        stripped = strip_stale_images(context)
        if stripped:
            await self._buddy.update_chat_ctx(context)
        self._buddy.set_guide_frame(frame.frame_id)

    async def _ack_frame(self, frame: ScreenFrame) -> None:
        """Publish a ``guide.step`` ack so the desktop frame handshake advances.

        Fires for EVERY accepted frame (change or forced), decoupled from replying.
        Fail-soft: the desktop's own 15s response timeout is the safety net if this
        never lands, so a dropped ack self-heals rather than stalling forever.
        """
        match = _GUIDE_FRAME_RE.fullmatch(frame.frame_id)
        if match is None or not self._active:
            return
        # Optimistic: mark acked before publish so a racing re-delivery dedups. On a
        # publish failure the desktop times out and releases the frame locally.
        self._last_acked_frame_id = frame.frame_id
        self._step_index += 1
        payload = json.dumps(
            {
                "type": GUIDE_STEP_TYPE,
                "payload": {
                    "frame_id": frame.frame_id,
                    "frame_seq": int(match.group(2)),
                    "step_index": self._step_index,
                    "instruction": "Screen checked.",
                    "done": False,
                },
            }
        ).encode("utf-8")
        try:
            await self._room.local_participant.publish_data(payload, reliable=True)
        except Exception as exc:
            logger.warn(
                "VoiceSession: Guide Mode acknowledgement failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "frame_id": frame.frame_id,
                    "error": str(exc),
                },
            )
