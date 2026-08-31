"""Natural, bounded screen guidance for an explicitly armed desktop session.

The desktop streams a fresh JPEG roughly every two seconds while Guide Mode is
armed, stamping each frame ``change:"1"`` when its own change-filter classified a
real visible change and ``change:"0"`` for a forced static-screen refresh. This
module does two things with that stream:

* Acks every received frame immediately with ``guide.frame_ack`` so the
  desktop's per-frame handshake never stalls and the next frame can flow.
  Acking is decoupled from replying.
* On a ``change:"1"`` frame it fires ONE terse proactive nudge ("Now click Save,
  top right") at a quiet turn boundary, debounced so it never stacks on a spoken
  reply or on a burst of rapid changes.

Spoken user questions are answered by ``GuideSupervisorAgent``. This
coordinator owns the native control handshake, same-session handoffs, frame
acking, proactive change nudges, and usage rollup.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Protocol

from livekit.agents import AgentSession
from livekit.agents import llm as lk_llm

from ...lib.logger import logger
from ...prompts import GUIDE_INSTRUCTIONS
from ...services.guide_usage_store import record_guide_usage
from .guide_session_state import GuidePhase, GuideStartClaim
from .guide_supervisor import GuideSupervisorAgent
from .guide_task_runtime import GuideTaskRuntime
from .interview import VoiceSessionState, interview_owns_conversation
from .screen_frames import ScreenFrame, ScreenFrameStore, strip_stale_images
from .transport import await_turn_boundary, publish_client_event

GUIDE_MODE_TYPE = "guide.mode"
GUIDE_HEARTBEAT_TYPE = "guide.heartbeat"
GUIDE_FRAME_ACK_TYPE = "guide.frame_ack"
GUIDE_MODE_ACK_TYPE = "guide.mode_ack"

_GUIDE_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
_GUIDE_FRAME_RE = re.compile(r"^([0-9a-f]{32}):(\d+)$")
_LISTENING_POLL_INTERVAL_S = 0.25
# A frame older than this no longer reflects "their screen right now", so it is
# never used to ground a proactive nudge.
_GUIDE_FRAME_MAX_AGE_S = 15.0
# Never fire two proactive nudges closer than this, however fast the screen churns.
_PROACTIVE_MIN_INTERVAL_S = 4.0
# Stay silent this long after the user actually spoke: the spoken turn already
# answered them, and their own action is what usually changed the screen.
_PROACTIVE_AFTER_TURN_QUIET_S = 2.0
_HANDOFF_TIMEOUT_S = 5.0


class GuideBuddy(Protocol):
    @property
    def chat_ctx(self) -> lk_llm.ChatContext: ...

    async def prepare_guide_resume(
        self,
        chat_ctx: lk_llm.ChatContext,
        ownership_epoch: int | None,
        ready: asyncio.Future[bool],
    ) -> None: ...


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
        task_runtime: GuideTaskRuntime,
        screen_frames: ScreenFrameStore,
        display_name: str,
    ) -> None:
        self._session = session
        self._buddy = buddy
        self._room = room
        self._session_id = session_id
        self._user_id = user_id
        self._task_runtime = task_runtime
        self._screen_frames = screen_frames
        self._display_name = display_name
        self._guide_agent: GuideSupervisorAgent | None = None
        self._active = False
        self._protocol_version = 2
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
        self._control_task: asyncio.Task | None = None
        self._closed = False
        self._task_runtime.bind_failure_handler(self.fail_closed)
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
            self._session.on("user_state_changed", self._on_user_state)
            self._session.on("agent_state_changed", self._on_agent_state)
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
        if self._control_task is not None:
            self._control_task.cancel()
            try:
                await self._control_task
            except asyncio.CancelledError:
                pass
            self._control_task = None
        await self._task_runtime.close()
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
                    "stage": "execution",
                    "outcome": "rejected",
                    "reason": "participant_identity_mismatch",
                },
            )
            return False
        active = message.get("active")
        generation = message.get("generation")
        guide_session_id = message.get("guide_session_id")
        protocol_version = message.get("protocol_version")
        resume_task_id = message.get("resume_task_id")
        if (
            not isinstance(active, bool)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
            or isinstance(protocol_version, bool)
            or not isinstance(protocol_version, int)
            or protocol_version != 2
            or (
                resume_task_id is not None
                and (
                    not isinstance(resume_task_id, str)
                    or _GUIDE_SESSION_RE.fullmatch(resume_task_id) is None
                )
            )
        ):
            return False
        if active and (
            not isinstance(guide_session_id, str)
            or _GUIDE_SESSION_RE.fullmatch(guide_session_id) is None
        ):
            return False
        if generation <= self._generation:
            return False

        state = getattr(self._session, "userdata", None)
        if not isinstance(state, VoiceSessionState):
            return False

        # Arming is refused while Interview Mode owns or is reserving the
        # conversation. Disarm is always honoured so screen capture can never be
        # trapped on by a cross-mode race.
        if active and (
            interview_owns_conversation(self._session)
            or state.interview.pending_start is not None
        ):
            logger.info(
                "VoiceSession: Guide arm refused, interview mode active",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "generation": generation,
                    "stage": "execution",
                    "outcome": "rejected",
                    "reason": "interview_mode_active",
                },
            )
            asyncio.create_task(
                self._publish_mode_ack(
                    active=False,
                    generation=generation,
                    guide_session_id=str(guide_session_id or ""),
                    protocol_version=protocol_version,
                    reason="interview_mode_active",
                ),
                name=f"guide-interview-nack-{self._session_id[:8]}",
            )
            return False

        claim: GuideStartClaim | None = None
        if active:
            claim = state.guide.claim_start(
                guide_session_id=guide_session_id,
                generation=generation,
                protocol_version=protocol_version,
                resume_task_id=resume_task_id,
            )
            if claim is None:
                asyncio.create_task(
                    self._publish_mode_ack(
                        active=False,
                        generation=generation,
                        guide_session_id=guide_session_id,
                        protocol_version=protocol_version,
                        reason="guide_transition_in_progress",
                    ),
                    name=f"guide-transition-nack-{self._session_id[:8]}",
                )
                return False

        previous_guide_session_id = self._guide_session_id
        self._generation = generation
        self._active = active
        self._protocol_version = protocol_version
        self._guide_session_id = guide_session_id if active else ""
        self._step_index = 0
        self._pending_nudge = False
        self._inflight_frame_id = ""
        self._last_acked_frame_id = ""
        if active:
            self._last_proactive_at = 0.0
            self._reset_usage()
        if not active:
            self._interrupt_guide_speech()
            self._latest_frame = None
            self._wake.clear()
            if previous_guide_session_id:
                asyncio.create_task(
                    self._flush_usage(previous_guide_session_id),
                    name=f"guide-usage-{self._session_id[:8]}",
                )
        transition_guide_session_id = (
            self._guide_session_id if active else previous_guide_session_id
        )
        self._schedule_control_transition(
            active=active,
            generation=generation,
            guide_session_id=transition_guide_session_id,
            protocol_version=protocol_version,
            reason=None,
            claim=claim,
        )
        logger.info(
            "VoiceSession: Guide Mode changed",
            {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "active": active,
                "generation": generation,
                "guide_session_id": self._guide_session_id,
                "protocol_version": protocol_version,
                "resume_task_id": resume_task_id,
            },
        )
        return True

    def fail_closed(self, reason: str) -> None:
        """Turn off the exact active generation after an unrecoverable Guide failure."""
        if not self._active or self._closed:
            return
        guide_session_id = self._guide_session_id
        self._active = False
        self._pending_nudge = False
        self._inflight_frame_id = ""
        self._latest_frame = None
        self._wake.clear()
        self._interrupt_guide_speech()
        self._schedule_control_transition(
            active=False,
            generation=self._generation,
            guide_session_id=guide_session_id,
            protocol_version=self._protocol_version,
            reason=reason,
            claim=None,
        )
        logger.warn(
            "VoiceSession: Guide Mode failed closed",
            {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "generation": self._generation,
                "guide_session_id": guide_session_id,
                "reason": reason,
                "stage": "planning",
                "outcome": "failed",
            },
        )

    def _interrupt_guide_speech(self) -> None:
        self._task_runtime.cancel_generation()
        if not self._task_runtime.speech_in_progress:
            return
        try:
            self._session.interrupt(force=True)
        except Exception as exc:
            logger.warn(
                "VoiceSession: Guide teardown interruption failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "error": str(exc),
                    "stage": "speech",
                    "outcome": "failed",
                    "reason": "teardown_interruption_failed",
                },
            )

    def _schedule_control_transition(
        self,
        *,
        active: bool,
        generation: int,
        guide_session_id: str,
        protocol_version: int,
        reason: str | None,
        claim: GuideStartClaim | None,
    ) -> None:
        if self._control_task is not None:
            self._control_task.cancel()
        self._control_task = asyncio.create_task(
            self._apply_control_transition(
                active=active,
                generation=generation,
                guide_session_id=guide_session_id,
                protocol_version=protocol_version,
                reason=reason,
                claim=claim,
            ),
            name=f"guide-control-{self._session_id[:8]}-{generation}",
        )

    async def _apply_control_transition(
        self,
        *,
        active: bool,
        generation: int,
        guide_session_id: str,
        protocol_version: int,
        reason: str | None,
        claim: GuideStartClaim | None,
    ) -> None:
        try:
            if active:
                if claim is None:
                    raise RuntimeError("missing_guide_start_claim")
                activation_ready = asyncio.get_running_loop().create_future()
                guide_agent = GuideSupervisorAgent(
                    state=self._session.userdata,
                    claim=claim,
                    chat_ctx=self._buddy.chat_ctx.copy(exclude_instructions=True),
                    screen_frames=self._screen_frames,
                    task_runtime=self._task_runtime,
                    user_id=self._user_id,
                    session_id=self._session_id,
                    display_name=self._display_name,
                    activation_ready=activation_ready,
                )
                self._guide_agent = guide_agent
                self._session.update_agent(guide_agent)
                activated = await asyncio.wait_for(
                    asyncio.shield(activation_ready), timeout=_HANDOFF_TIMEOUT_S
                )
                if (
                    self._closed
                    or generation != self._generation
                    or not self._active
                    or guide_session_id != self._guide_session_id
                ):
                    return
                if not activated:
                    raise RuntimeError("guide_supervisor_not_activated")
            else:
                await self._task_runtime.deactivate(cancelled=reason is None)
                state = self._session.userdata
                guide = state.guide
                resume_epoch: int | None = None
                pending = guide.pending_start
                if pending is not None:
                    guide.cancel_start(pending, reason or "native_disarm")
                elif guide.phase is not GuidePhase.IDLE:
                    if guide.phase is not GuidePhase.RETURN_PENDING:
                        guide.request_return(reason or "native_disarm")
                    resume_epoch = guide.ownership_epoch
                resumed = await self._resume_buddy(resume_epoch)
                if not resumed:
                    raise RuntimeError("buddy_not_activated")
                self._guide_agent = None
                if self._closed or generation != self._generation:
                    return
            await self._publish_mode_ack(
                active=active,
                generation=generation,
                guide_session_id=guide_session_id,
                protocol_version=protocol_version,
                reason=reason,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warn(
                "VoiceSession: Guide control transition failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "generation": generation,
                    "active": active,
                    "error_type": type(exc).__name__,
                    "stage": "execution",
                    "outcome": "failed",
                    "reason": "control_transition_failed",
                },
            )
            if active and generation == self._generation:
                self._active = False
                await self._task_runtime.deactivate(cancelled=True)
                await self._recover_failed_activation(claim)
                # A reservation that never committed must not consume the
                # desktop generation. If entry did commit before failing, the
                # session state keeps that generation and requires the next one.
                self._generation = self._session.userdata.guide.generation
                self._guide_session_id = ""
                await self._publish_mode_ack(
                    active=False,
                    generation=generation,
                    guide_session_id=guide_session_id,
                    protocol_version=protocol_version,
                    reason="activation_failed",
                )
        finally:
            if self._control_task is asyncio.current_task():
                self._control_task = None

    async def _recover_failed_activation(
        self, claim: GuideStartClaim | None
    ) -> None:
        """Restore Buddy after a supervisor reservation or activation failed."""
        state = self._session.userdata
        guide = state.guide
        resume_epoch: int | None = None
        if claim is not None and guide.pending_start == claim:
            guide.cancel_start(claim, "activation_failed")
        elif guide.phase is not GuidePhase.IDLE:
            if guide.phase is not GuidePhase.RETURN_PENDING:
                guide.request_return("activation_failed")
            resume_epoch = guide.ownership_epoch
        try:
            await self._resume_buddy(resume_epoch)
        except Exception as exc:
            logger.warn(
                "GuideCoordinator: Buddy recovery failed",
                {
                    "session_id": self._session_id,
                    "error_type": type(exc).__name__,
                },
            )
        self._guide_agent = None

    async def _resume_buddy(self, ownership_epoch: int | None) -> bool:
        chat_ctx = (
            self._guide_agent.chat_ctx.copy(exclude_instructions=True)
            if self._guide_agent is not None
            else self._buddy.chat_ctx.copy(exclude_instructions=True)
        )
        ready = asyncio.get_running_loop().create_future()
        await self._buddy.prepare_guide_resume(chat_ctx, ownership_epoch, ready)
        self._session.update_agent(self._buddy)
        return await asyncio.wait_for(
            asyncio.shield(ready), timeout=_HANDOFF_TIMEOUT_S
        )

    async def _publish_mode_ack(
        self,
        *,
        active: bool,
        generation: int,
        guide_session_id: str,
        protocol_version: int,
        reason: str | None,
    ) -> None:
        published = await publish_client_event(
            self._room,
            GUIDE_MODE_ACK_TYPE,
            {
                "active": active,
                "generation": generation,
                "guide_session_id": guide_session_id,
                "protocol_version": protocol_version,
                "reason": reason,
            },
            log_message="VoiceSession: Guide mode acknowledgement failed",
            log_fields={
                "session_id": self._session_id,
                "user_id": self._user_id,
                "generation": generation,
                "active": active,
                "stage": "execution",
                "outcome": "failed",
                "reason": "mode_ack_publish_failed",
            },
        )
        if published:
            logger.info(
                "GuideTrace",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "guide_session_id": guide_session_id,
                    "generation": generation,
                    "protocol_version": protocol_version,
                    "active": active,
                    "stage": "execution",
                    "outcome": "succeeded",
                    "reason": "mode_ack_published",
                },
            )

    def submit_frame(self, frame: ScreenFrame) -> None:
        """Ack every accepted frame; a ``change:"1"`` frame also wakes a nudge."""
        if frame.attributes.get("mode") != "guide":
            self._log_frame_drop(frame, "wrong_mode")
            self._reject_frame(frame, "wrong_mode")
            return
        match = _GUIDE_FRAME_RE.fullmatch(frame.frame_id)
        if match is None:
            self._log_frame_drop(frame, "invalid_frame_id")
            self._reject_frame(frame, "invalid_frame_id")
            return
        if not self._active:
            self._log_frame_drop(frame, "inactive_session")
            self._reject_frame(frame, "inactive_session")
            return
        if match.group(1) != self._guide_session_id:
            self._log_frame_drop(frame, "wrong_session")
            self._reject_frame(frame, "wrong_session")
            return
        self._latest_frame = frame
        if frame.frame_id == self._last_acked_frame_id:
            # A re-delivery of a frame we already acked (desktop response retry);
            # never double-ack or re-nudge it.
            self._log_frame_drop(frame, "already_acked")
            asyncio.create_task(self._ack_frame(frame), name=f"guide-reack-{self._session_id[:8]}")
            return
        # Ack fast and decoupled from replying so the desktop handshake never stalls.
        asyncio.create_task(self._ack_frame(frame), name=f"guide-ack-{self._session_id[:8]}")
        self._task_runtime.note_activity()
        if frame.attributes.get("change") == "1" or frame.attributes.get("capture_reason") in {
            "verification_timeout",
            "resume",
            "explicit_look",
            "app_window_change",
            "geometry_change",
        }:
            self._pending_nudge = True
            self._wake.set()

    def apply_heartbeat(self, message: dict, participant_identity: str) -> bool:
        if participant_identity != self._user_id or not self._active:
            return False
        if message.get("guide_session_id") != self._guide_session_id:
            return False
        return True

    def _reject_frame(self, frame: ScreenFrame, reason: str) -> None:
        asyncio.create_task(
            self._publish_frame_ack(frame, accepted=False, reason=reason),
            name=f"guide-reject-{self._session_id[:8]}",
        )

    def _on_user_state(self, event) -> None:
        if not self._active or str(getattr(event, "new_state", "")) != "speaking":
            return
        self._pending_nudge = False
        self._wake.clear()
        asyncio.create_task(
            self._task_runtime.on_user_speech_start(),
            name=f"guide-interrupt-{self._session_id[:8]}",
        )
        try:
            self._session.interrupt(force=True)
        except Exception as exc:
            logger.warn(
                "VoiceSession: Guide interruption failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "error": str(exc),
                    "stage": "speech",
                    "outcome": "failed",
                    "reason": "user_interruption_failed",
                },
            )

    def _on_agent_state(self, event) -> None:
        if not self._active:
            return
        state = str(getattr(event, "new_state", ""))
        asyncio.create_task(
            self._task_runtime.on_agent_state(state),
            name=f"guide-agent-state-{self._session_id[:8]}",
        )

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
        logger.info(
            "VoiceSession: Guide Mode frame dropped",
            {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "frame_id": frame.frame_id,
                "reason": reason,
            },
        )

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
        """Wait for a quiet moment. Unlike the other callers this also waits out
        the user speaking: a proactive nudge must never talk over them."""
        await await_turn_boundary(
            self._session,
            poll_s=_LISTENING_POLL_INTERVAL_S,
            require_user_idle=True,
        )

    async def _run(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            if not self._active or not self._pending_nudge:
                continue
            self._pending_nudge = False
            # A proactive nudge is unsolicited speech. While Interview Mode owns
            # the conversation it would land on the intake task and answer a
            # screen change instead of the question it just asked. Dropped, not
            # queued: by the time the interview ends the screen it described is
            # long gone.
            if interview_owns_conversation(self._session):
                continue

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
                guide_agent = self._guide_agent
                if guide_agent is None:
                    continue
                await self._prepare_context(frame)
                proactive_message_id = f"guide-proactive-{frame.frame_id}"
                guide_agent.prepare_proactive_turn(
                    message_id=proactive_message_id,
                    frame_id=frame.frame_id,
                    model_scale=frame.model_scale,
                    turn_context_id=frame.turn_context_id,
                )
                message = lk_llm.ChatMessage(
                    id=proactive_message_id,
                    role="user",
                    content=[
                        "The currently visible screen changed while Guide Mode is active.",
                        # Pinned, not left at LiveKit's "auto": see the same note in
                        # screen_frames.attach_screen_frame_to_turn. Guide reads small
                        # UI labels, so provider-chosen fidelity is not acceptable.
                        lk_llm.ImageContent(
                            image=(
                                "data:image/jpeg;base64,"
                                + base64.b64encode(frame.jpeg_bytes).decode("ascii")
                            ),
                            mime_type="image/jpeg",
                            inference_detail="high",
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
                        "stage": "speech",
                        "outcome": "failed",
                        "reason": "proactive_speech_failed",
                        "trace_id": frame.attributes.get("trace_id") or None,
                        "event_id": frame.attributes.get("event_id") or None,
                    },
                )
            finally:
                self._inflight_frame_id = ""

    async def _prepare_context(self, frame: ScreenFrame) -> None:
        guide_agent = self._guide_agent
        if guide_agent is None:
            return
        context = guide_agent.chat_ctx.copy()
        stripped = strip_stale_images(context)
        if stripped:
            await guide_agent.update_chat_ctx(context)

    async def _ack_frame(self, frame: ScreenFrame) -> None:
        """Publish a ``guide.frame_ack`` so the desktop handshake advances.

        Fires for EVERY accepted frame (change or forced), decoupled from replying.
        Fail-soft: the desktop's own 15s response timeout is the safety net if this
        never lands, so a dropped ack self-heals rather than stalling forever.
        """
        match = _GUIDE_FRAME_RE.fullmatch(frame.frame_id)
        if match is None or not self._active:
            return
        await self._publish_frame_ack(frame, accepted=True, reason=None)

    async def _publish_frame_ack(
        self,
        frame: ScreenFrame,
        *,
        accepted: bool,
        reason: str | None,
    ) -> None:
        match = _GUIDE_FRAME_RE.fullmatch(frame.frame_id)
        sequence = int(match.group(2)) if match else frame.sequence
        newest = self._latest_frame.frame_id if self._latest_frame else ""
        published = await publish_client_event(
            self._room,
            GUIDE_FRAME_ACK_TYPE,
            {
                "frame_id": frame.frame_id,
                "frame_seq": sequence,
                "accepted": accepted,
                "rejection_reason": reason,
                "newest_frame_id": newest,
            },
            log_message="VoiceSession: Guide frame acknowledgement failed",
            log_fields={
                "session_id": self._session_id,
                "user_id": self._user_id,
                "frame_id": frame.frame_id,
                "accepted": accepted,
                "stage": "execution",
                "outcome": "failed",
                "reason": "frame_ack_publish_failed",
                "trace_id": frame.attributes.get("trace_id") or None,
                "event_id": frame.attributes.get("event_id") or None,
            },
        )
        if published and accepted and self._last_acked_frame_id != frame.frame_id:
            self._last_acked_frame_id = frame.frame_id
            self._step_index += 1
