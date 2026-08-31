"""Durable, deterministic Guide task orchestration behind the Guide supervisor."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from livekit.agents import llm as lk_llm

from ...lib.logger import logger
from ...services.guide_task_store import (
    GuideTaskConflictError,
    GuideTaskLeaseError,
    GuideTaskStore,
)
from .chat_context import latest_user_text
from .guide_kernel import (
    GuideDecisionProvider,
    GuideFrameInput,
    GuideKernel,
    GuideStage,
    GuideTaskProfile,
    GuideTaskRepository,
    GuideTraceContext,
    GuideTraceOutcome,
)
from .guide_models import (
    GuideDecisionKind,
    GuideEvidence,
    GuideInstruction,
    GuideInstructionStatus,
    GuideStepStatus,
    GuideTask,
    GuideTaskStatus,
    GuideTaskStep,
    GuideVisualDecision,
)
from .guide_prompt import (
    GUIDE_DECISION_PROMPT_VERSION,
    GUIDE_PLANNER_PROMPT_VERSION,
)
from .point_tag import PointTarget, publish_element_point
from .screen_frames import ScreenFrame, ScreenFrameStore
from .transport import publish_client_event

_TERMINAL_STATES = {GuideTaskStatus.COMPLETED, GuideTaskStatus.CANCELLED}
_CURRENT_APP = "the current app"
_CURRENT_APP_ALIASES = frozenset(
    {
        "current app",
        "current application",
        "the current app",
        "the current application",
        "unknown",
        "unknown app",
        "unknown application",
    }
)


def _kernel_frame(frame: ScreenFrame) -> GuideFrameInput:
    return GuideFrameInput(
        frame_id=frame.frame_id,
        image_bytes=frame.jpeg_bytes,
        width_px=frame.width_px,
        height_px=frame.height_px,
        active_process=frame.active_process,
        active_window_id=frame.active_window_id,
        geometry_revision=frame.geometry_revision,
        age_seconds=frame.age_seconds,
        metadata=frame.semantic_metadata,
    )


def _spoken_target_app(target_app: str) -> str:
    normalized = " ".join(target_app.replace("_", " ").split())
    canonical = re.sub(r"[^a-z0-9]+", " ", normalized.casefold()).strip()
    if not canonical or canonical in _CURRENT_APP_ALIASES:
        return _CURRENT_APP
    return normalized


def _resolved_target_app(target_app: str, frame: ScreenFrame) -> str:
    normalized = _spoken_target_app(target_app)
    if normalized != _CURRENT_APP:
        return normalized
    return (
        frame.active_process.strip()
        or frame.attributes.get("active_window_title", "").strip()
        or "unavailable foreground application"
    )


class GuideTaskRuntime:
    def __init__(
        self,
        *,
        user_id: str,
        voice_session_id: str,
        screen_frames: ScreenFrameStore,
        room,
        session,
        profile: GuideTaskProfile,
        decision_provider: GuideDecisionProvider,
        store: GuideTaskRepository | None = None,
    ) -> None:
        self._user_id = user_id
        self._voice_session_id = voice_session_id
        self._screen_frames = screen_frames
        self._room = room
        self._session = session
        self._store = store or GuideTaskStore()
        self._profile = profile
        self._kernel = GuideKernel(profile)
        self._decisions = decision_provider
        self._run_id = uuid.uuid4().hex
        self._lease_owner = f"{voice_session_id}:{self._run_id}"
        self._guide_session_id = ""
        self._protocol_version = 2
        self._resume_task_id = ""
        self._task: GuideTask | None = None
        self._speech_epoch = 0
        self._decision_task: asyncio.Task | None = None
        self._lease_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._state_lock = asyncio.Lock()
        self._active = False
        self._last_spoken_instruction_id = ""
        self._speech_started = False
        self._last_activity_at = time.monotonic()
        self._turn_started_at = 0.0
        self._failure_handler: Callable[[str], None] | None = None
        self._session_closer: Callable[[str], Awaitable[None]] | None = None
        self._current_trace = GuideTraceContext(
            trace_id=uuid.uuid4().hex,
            event_id=uuid.uuid4().hex,
        )

    @property
    def task(self) -> GuideTask | None:
        return self._task

    @property
    def speech_in_progress(self) -> bool:
        return self._speech_started

    def bind_failure_handler(self, handler: Callable[[str], None]) -> None:
        self._failure_handler = handler

    def bind_session_closer(self, closer: Callable[[str], Awaitable[None]]) -> None:
        """Attach the one owner allowed to end the voice session.

        Guide can decide the session is dead, but it must not be the thing that
        closes it: the owner stamps the reason the post-session pipeline reads.
        Unbound, this falls back to closing directly so a runtime built outside
        the worker still terminates rather than idling forever.
        """
        self._session_closer = closer

    def should_delegate(self) -> bool:
        return self._active

    def diagnostic_state(self) -> dict[str, object]:
        return {
            "runtime_active": self._active,
            "protocol_version": self._protocol_version,
            "guide_session_id": self._guide_session_id or None,
            "task_id": self._task.task_id if self._task else None,
            "task_revision": self._task.revision if self._task else None,
            "task_profile_id": self._profile.profile_id,
            "decision_provider": self._decisions.provider_id,
        }

    def activate(
        self,
        *,
        guide_session_id: str,
        protocol_version: int,
        resume_task_id: str | None,
    ) -> None:
        self._active = True
        self._guide_session_id = guide_session_id
        self._protocol_version = protocol_version
        self._resume_task_id = resume_task_id or ""
        self.note_activity()
        self._start_idle_watchdog()

    def note_activity(self) -> None:
        self._last_activity_at = time.monotonic()

    async def deactivate(self, *, cancelled: bool) -> None:
        self._active = False
        self.cancel_generation()
        async with self._state_lock:
            if not self._task or self._task.status in _TERMINAL_STATES:
                return
            if not cancelled and self._task.status == GuideTaskStatus.BLOCKED:
                return
            target = GuideTaskStatus.CANCELLED if cancelled else GuideTaskStatus.PAUSED_OFFLINE
            reason = "explicit_cancel" if cancelled else "voice_session_ended"
            await self._mutate_status(target, reason)

    async def close(self) -> None:
        await self.deactivate(cancelled=False)
        if self._lease_task is not None:
            self._lease_task.cancel()
            try:
                await self._lease_task
            except asyncio.CancelledError:
                pass
            self._lease_task = None
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None

    def cancel_generation(self) -> None:
        self._speech_epoch += 1
        current = self._decision_task
        if current is not None and current is not asyncio.current_task():
            current.cancel()

    async def on_user_speech_start(self) -> None:
        self.cancel_generation()
        async with self._state_lock:
            if self._speech_started:
                await self._mark_instruction(GuideInstructionStatus.INTERRUPTED)
                await self._trace(
                    GuideStage.SPEECH,
                    GuideTraceOutcome.REJECTED,
                    task=self._task,
                    reason="user_interrupted",
                )
        logger.info(
            "GuideTelemetry: user interruption",
            self._correlation(),
        )

    async def on_agent_state(self, state: str) -> None:
        async with self._state_lock:
            if state == "speaking" and self._last_spoken_instruction_id:
                self._speech_started = True
                await self._mark_instruction(GuideInstructionStatus.SPEECH_STARTED)
                if self._turn_started_at:
                    logger.info(
                        "GuideTelemetry: first audio",
                        {
                            **self._correlation(),
                            "end_of_turn_to_first_audio_ms": round(
                                (time.monotonic() - self._turn_started_at) * 1000
                            ),
                        },
                    )
            elif state == "listening" and self._speech_started and self._last_spoken_instruction_id:
                self._speech_started = False
                await self._mark_instruction(GuideInstructionStatus.DELIVERED)
                await self._trace(
                    GuideStage.SPEECH,
                    GuideTraceOutcome.SUCCEEDED,
                    task=self._task,
                    delivery_status=GuideInstructionStatus.DELIVERED,
                )
            elif state == "failed" and self._last_spoken_instruction_id:
                self._speech_started = False
                await self._mark_instruction(GuideInstructionStatus.DELIVERY_UNKNOWN)
                await self._trace(
                    GuideStage.SPEECH,
                    GuideTraceOutcome.FAILED,
                    task=self._task,
                    reason="agent_speech_failed",
                )

    async def generate(
        self,
        chat_ctx: lk_llm.ChatContext,
        *,
        current_turn_context_id: str = "",
        proactive: bool = False,
        force_replan: bool = False,
    ) -> str:
        if not self._active:
            return ""
        self._turn_started_at = time.monotonic()
        self.cancel_generation()
        epoch = self._speech_epoch
        task = asyncio.create_task(
            self._process(
                chat_ctx,
                epoch,
                current_turn_context_id,
                proactive=proactive,
                force_replan=force_replan,
            ),
            name=f"guide-decision-{self._voice_session_id[:8]}",
        )
        self._decision_task = task
        try:
            return await task
        except asyncio.CancelledError:
            return ""
        finally:
            if self._decision_task is task:
                self._decision_task = None

    async def _process(
        self,
        chat_ctx: lk_llm.ChatContext,
        speech_epoch: int,
        current_turn_context_id: str = "",
        *,
        proactive: bool = False,
        force_replan: bool = False,
    ) -> str:
        raw_transcript = latest_user_text(chat_ctx)
        transcript = "" if proactive else raw_transcript
        frame = await self._screen_frames.fresh_frame(
            current_turn_context_id=current_turn_context_id
        )
        if frame is None:
            await self._trace(
                GuideStage.CAPTURE,
                GuideTraceOutcome.FAILED,
                reason="fresh_frame_unavailable",
            )
            return "I need one fresh look before we continue." if transcript else ""
        if transcript:
            capture_event_id = frame.attributes.get("event_id", "")
            frame.attributes["parent_event_id"] = capture_event_id
            frame.attributes["event_id"] = uuid.uuid4().hex
        self._current_trace = self._trace_for(frame=frame)
        await self._trace(
            GuideStage.CAPTURE,
            GuideTraceOutcome.SUCCEEDED,
            frame=frame,
            capture_reason=frame.attributes.get("capture_reason", ""),
        )

        async with self._state_lock:
            self.note_activity()
            had_task = self._task is not None
            try:
                task = await self._ensure_task(transcript, frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._trace(
                    GuideStage.EXECUTION,
                    GuideTraceOutcome.FAILED,
                    frame=frame,
                    task=self._task,
                    reason="task_load_or_create_failed",
                    error=exc,
                )
                return ""
            if task is None:
                return ""
            replanned = False
            if task.status == GuideTaskStatus.BLOCKED and task.blocked_reason == "blocked_provider":
                if not transcript or not had_task:
                    return (
                        "I couldn't plan that just now. Ask me again in a moment."
                        if transcript
                        else ""
                    )
                task = await self._replan_task(task, transcript, frame)
                replanned = True
                if task.status == GuideTaskStatus.BLOCKED:
                    # A planning-provider failure is an interpretation-lane error, not
                    # a reason to tear the mode down. Stay armed in the Guide supervisor
                    # and let the next user turn retry; the switch (Guide on/off) is
                    # owned only by the user hotkey, sign-out, and durable completion.
                    # This previously called the fail-closed handler after two failures,
                    # which disarmed Guide and silently dropped the user back into
                    # normal Buddy mid-task. The task stays BLOCKED/blocked_provider and
                    # self-heals when the provider recovers. fail_closed remains for a
                    # genuine unrecoverable switch-lane failure, never for this path.
                    return "I couldn't plan that just now. Ask me again in a moment."
            if force_replan and transcript and had_task and not replanned:
                task = await self._replan_task(task, transcript, frame)
            elif task.status in {
                GuideTaskStatus.CLARIFYING,
                GuideTaskStatus.REPLANNING,
            }:
                if not transcript:
                    return ""
                task = await self._replan_task(task, transcript, frame)
            if task.status == GuideTaskStatus.CLARIFYING:
                return task.clarification_question or (
                    "What outcome should I help you complete?" if transcript else ""
                )
            if not self._frame_authorized(frame, task):
                # Break the single authorization bool into its raw inputs so a false
                # PAUSED_APP ("Bring CapCut back" while CapCut is on screen) can be
                # confirmed from logs. app_matched is the substring token match in
                # the profile; if it is False while active_process is a browser and
                # the window title names the app, the target ran in a browser tab.
                await self._trace(
                    GuideStage.VERIFICATION,
                    GuideTraceOutcome.REJECTED,
                    frame=frame,
                    task=task,
                    reason="target_application_or_authorization_mismatch",
                    active_process=frame.active_process,
                    active_window_title=frame.attributes.get("active_window_title", ""),
                    target_app=task.target_app,
                    app_matched=self._profile.matches_active_app(
                        frame.active_process, task.target_app
                    ),
                    frame_age_s=round(frame.age_seconds, 2),
                    guide_session_matched=(
                        frame.attributes.get("guide_session_id") == self._guide_session_id
                    ),
                )
                await self._mutate_status(
                    GuideTaskStatus.PAUSED_APP,
                    "target application is not active",
                )
                return (
                    f"Bring {_spoken_target_app(task.target_app)} back, then I'll continue."
                    if transcript
                    else ""
                )
            current_step = task.current_step
            if current_step is None:
                return ""
            if current_step.attempt_count >= 2 and not current_step.verified_evidence_refs:
                await self._mutate_status(
                    GuideTaskStatus.REPLANNING,
                    "same step repeated without new evidence",
                )
                return "Tell me what you see now, and I'll reorient."
            try:
                task = await self._checkpoint_observation(task, frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._trace(
                    GuideStage.EXECUTION,
                    GuideTraceOutcome.FAILED,
                    frame=frame,
                    task=task,
                    reason="observation_checkpoint_failed",
                    error=exc,
                )
                return ""
            input_revision = task.revision

        correlation = self._correlation(frame=frame, task=task)
        await self._trace(
            GuideStage.VERIFICATION,
            GuideTraceOutcome.STARTED,
            frame=frame,
            task=task,
        )
        try:
            decision = await self._decisions.decide(
                task,
                _kernel_frame(frame),
                transcript,
                profile=self._profile,
                correlation=correlation,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._trace(
                GuideStage.VERIFICATION,
                GuideTraceOutcome.FAILED,
                frame=frame,
                task=task,
                reason="decision_provider_unavailable",
                error=exc,
            )
            await self._mutate_status(
                GuideTaskStatus.BLOCKED,
                "verification provider unavailable",
            )
            return "I couldn't verify this screen. Ask me to look again."

        async with self._state_lock:
            if (
                speech_epoch != self._speech_epoch
                or not self._active
                or self._task is None
                or self._task.revision != input_revision
            ):
                logger.info(
                    "GuideTelemetry: stale decision rejected",
                    {**correlation, "reason": "epoch_or_revision_changed"},
                )
                await self._trace(
                    GuideStage.VERIFICATION,
                    GuideTraceOutcome.REJECTED,
                    frame=frame,
                    task=self._task,
                    reason="epoch_or_revision_changed",
                )
                return ""
            try:
                spoken = await self._apply_decision(
                    decision,
                    frame=frame,
                    transcript=transcript,
                    input_revision=input_revision,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._trace(
                    GuideStage.EXECUTION,
                    GuideTraceOutcome.FAILED,
                    frame=frame,
                    task=self._task,
                    reason="decision_application_failed",
                    error=exc,
                )
                await self._mutate_status(
                    GuideTaskStatus.BLOCKED,
                    "task transition failed",
                )
                return ""
            await self._trace(
                GuideStage.EXECUTION,
                GuideTraceOutcome.SUCCEEDED,
                frame=frame,
                task=self._task,
                spoken=bool(spoken),
            )
            return spoken

    async def _checkpoint_observation(
        self,
        task: GuideTask,
        frame: ScreenFrame,
    ) -> GuideTask:
        expected = task.revision

        def _reduce(updated: GuideTask) -> GuideTask:
            updated.status = GuideTaskStatus.VERIFYING
            updated.last_frame_id = frame.frame_id
            updated.last_active_app = frame.active_process
            return updated

        self._task = await self._store.mutate(
            self._user_id,
            task.task_id,
            self._lease_owner,
            expected,
            _reduce,
        )
        await self._publish_task()
        return self._task

    async def _ensure_task(
        self,
        transcript: str,
        frame: ScreenFrame,
    ) -> GuideTask | None:
        if self._task is not None:
            return self._task
        if self._resume_task_id:
            resumed = await self._store.load(self._user_id, self._resume_task_id)
            profile_matches = bool(
                resumed is not None
                and resumed.task_profile_id
                in {"legacy", self._profile.profile_id}
            )
            if resumed is not None and resumed.resumable and profile_matches:
                self._task = await self._store.acquire_lease(
                    self._user_id,
                    resumed.task_id,
                    self._lease_owner,
                    resumed=True,
                )
                self._start_lease_renewal()
                await self._publish_task()
                logger.info(
                    "GuideTelemetry: task resumed",
                    self._correlation(frame=frame, task=self._task),
                )
                return self._task
        if not transcript:
            return None
        correlation = self._correlation(frame=frame)
        planner_failed = False
        await self._trace(
            GuideStage.PLANNING,
            GuideTraceOutcome.STARTED,
            frame=frame,
        )
        try:
            plan = await self._decisions.plan(
                transcript,
                existing_task=None,
                profile=self._profile,
                correlation=correlation,
            )
        except Exception as exc:
            planner_failed = True
            await self._trace(
                GuideStage.PLANNING,
                GuideTraceOutcome.FAILED,
                frame=frame,
                reason="planning_provider_unavailable",
                error=exc,
            )
            logger.warn(
                "GuideTelemetry: planner unavailable",
                {**correlation, "error_type": type(exc).__name__},
            )
            plan = self._profile.fallback_plan(transcript)
        else:
            await self._trace(
                GuideStage.PLANNING,
                GuideTraceOutcome.SUCCEEDED,
                frame=frame,
            )
        now = datetime.now(UTC)
        task_shape = self._kernel.task_shape(plan)
        steps = []
        for source in task_shape.steps:
            task_step = source.model_dump()
            task_step["status"] = GuideStepStatus.ACTIVE if not steps else GuideStepStatus.PENDING
            steps.append(task_step)
        task = GuideTask(
            task_id=uuid.uuid4().hex,
            user_id=self._user_id,
            task_profile_id=task_shape.task_profile_id,
            task_profile_version=task_shape.task_profile_version,
            goal=plan.goal,
            target_app=_resolved_target_app(task_shape.target_app, frame),
            constraints=task_shape.constraints,
            acceptance_criteria=task_shape.acceptance_criteria,
            steps=steps,
            current_step_id=None if planner_failed else steps[0]["step_id"],
            status=(
                GuideTaskStatus.BLOCKED
                if planner_failed
                else GuideTaskStatus.CLARIFYING
                if plan.clarification_question
                else GuideTaskStatus.ACTIVE
            ),
            clarification_question=plan.clarification_question,
            blocked_reason="blocked_provider" if planner_failed else None,
            lease_owner=self._lease_owner,
            lease_expires_at=now,
            planner_prompt_version=GUIDE_PLANNER_PROMPT_VERSION,
            guide_prompt_version=GUIDE_DECISION_PROMPT_VERSION,
            model_id=self._decisions.provider_id,
            planning_calls=1,
            created_at=now,
            updated_at=now,
        )
        # The store owns the canonical 30 second lease value.
        task.lease_expires_at = now
        created = await self._store.create(task)
        self._task = await self._store.acquire_lease(
            self._user_id,
            created.task_id,
            self._lease_owner,
            resumed=False,
        )
        self._start_lease_renewal()
        await self._publish_task()
        logger.info(
            "GuideTelemetry: task created",
            {
                **self._correlation(frame=frame, task=self._task),
                "task_profile_id": self._profile.profile_id,
                "task_profile_version": self._profile.profile_version,
            },
        )
        return self._task

    async def _replan_task(
        self,
        task: GuideTask,
        transcript: str,
        frame: ScreenFrame,
    ) -> GuideTask:
        await self._trace(
            GuideStage.PLANNING,
            GuideTraceOutcome.STARTED,
            frame=frame,
            task=task,
            replan=True,
        )
        try:
            plan = await self._decisions.plan(
                transcript,
                existing_task=task,
                profile=self._profile,
                correlation=self._correlation(frame=frame, task=task),
            )
        except Exception as exc:
            await self._trace(
                GuideStage.PLANNING,
                GuideTraceOutcome.FAILED,
                frame=frame,
                task=task,
                reason="replanning_provider_unavailable",
                error=exc,
            )
            expected = task.revision

            def _reduce_failed(updated: GuideTask) -> GuideTask:
                updated.status = GuideTaskStatus.BLOCKED
                updated.blocked_reason = "blocked_provider"
                updated.current_step_id = None
                updated.planning_calls += 1
                return updated

            try:
                self._task = await self._store.mutate(
                    self._user_id,
                    task.task_id,
                    self._lease_owner,
                    expected,
                    _reduce_failed,
                )
                await self._publish_task()
            except (GuideTaskConflictError, GuideTaskLeaseError):
                self._task = await self._store.load(self._user_id, task.task_id)
            logger.warn(
                "GuideTelemetry: replan unavailable",
                {
                    **self._correlation(frame=frame, task=self._task or task),
                    "error_type": type(exc).__name__,
                    "planning_calls": (self._task or task).planning_calls,
                },
            )
            return self._task or task
        await self._trace(
            GuideStage.PLANNING,
            GuideTraceOutcome.SUCCEEDED,
            frame=frame,
            task=task,
            replan=True,
        )
        task_shape = self._kernel.task_shape(plan)
        expected = task.revision

        def _reduce(updated: GuideTask) -> GuideTask:
            previous_by_id = {step.step_id: step for step in updated.steps}
            rebuilt_steps: list[GuideTaskStep] = []
            active_assigned = False
            for planned in task_shape.steps:
                previous = previous_by_id.get(planned.step_id)
                step_data = planned.model_dump()
                if previous is not None and previous.status == GuideStepStatus.VERIFIED:
                    step_data.update(
                        status=GuideStepStatus.VERIFIED,
                        attempt_count=previous.attempt_count,
                        last_instruction_id=previous.last_instruction_id,
                        verified_evidence_refs=previous.verified_evidence_refs,
                    )
                elif not active_assigned:
                    step_data["status"] = GuideStepStatus.ACTIVE
                    active_assigned = True
                else:
                    step_data["status"] = GuideStepStatus.PENDING
                rebuilt_steps.append(GuideTaskStep.model_validate(step_data))

            updated.goal = plan.goal
            updated.target_app = _resolved_target_app(
                task_shape.target_app, frame
            )
            updated.constraints = task_shape.constraints
            updated.acceptance_criteria = task_shape.acceptance_criteria
            updated.steps = rebuilt_steps
            updated.current_step_id = next(
                (
                    step.step_id
                    for step in rebuilt_steps
                    if step.status == GuideStepStatus.ACTIVE
                ),
                None,
            )
            updated.plan_revision += 1
            updated.planning_calls += 1
            updated.clarification_question = plan.clarification_question
            updated.status = (
                GuideTaskStatus.CLARIFYING
                if plan.clarification_question
                else GuideTaskStatus.ACTIVE
            )
            updated.blocked_reason = None
            return updated

        self._task = await self._store.mutate(
            self._user_id,
            task.task_id,
            self._lease_owner,
            expected,
            _reduce,
        )
        await self._publish_task()
        return self._task

    def _frame_authorized(self, frame: ScreenFrame, task: GuideTask) -> bool:
        kernel_frame = _kernel_frame(frame)
        authorization = {
            "user_id": self._user_id,
            "lease_owner": self._lease_owner,
            "guide_session_id": self._guide_session_id,
        }
        if self._kernel.frame_authorized(
            kernel_frame,
            task,
            **authorization,
        ):
            return True
        active_window_title = frame.attributes.get("active_window_title", "").strip()
        return bool(
            active_window_title
            and self._kernel.frame_authorized(
                replace(kernel_frame, active_process=active_window_title),
                task,
                **authorization,
            )
        )

    async def _apply_decision(
        self,
        decision: GuideVisualDecision,
        *,
        frame: ScreenFrame,
        transcript: str,
        input_revision: int,
    ) -> str:
        assert self._task is not None
        task = self._task
        step = task.current_step
        if step is None:
            return ""
        decision.observation.observation_id = hashlib.sha256(
            (
                f"{task.task_id}|{input_revision}|{frame.frame_id}|{decision.observation.summary}"
            ).encode()
        ).hexdigest()[:24]
        visible = {control.control_id: control for control in decision.observation.visible_controls}
        target = visible.get(decision.target_control_id) if decision.target_control_id else None
        if (
            decision.frame_id != frame.frame_id
            or decision.active_window_id != frame.active_window_id
            or decision.geometry_revision != frame.geometry_revision
        ):
            logger.info(
                "GuideTelemetry: stale decision rejected",
                {
                    **self._correlation(frame=frame, task=task),
                    "reason": "frame_window_or_geometry_mismatch",
                },
            )
            await self._trace(
                GuideStage.VERIFICATION,
                GuideTraceOutcome.REJECTED,
                frame=frame,
                task=task,
                reason="frame_window_or_geometry_mismatch",
            )
            return ""
        if decision.target_control_id and target is None:
            await self._trace(
                GuideStage.VERIFICATION,
                GuideTraceOutcome.REJECTED,
                frame=frame,
                task=task,
                reason="target_control_not_visible",
            )
            return ""
        if target is not None:
            bounds = target.bounds
            if (
                frame.age_seconds > 2.0
                or bounds.x < 0
                or bounds.y < 0
                or bounds.x + bounds.width > (frame.width_px or 0)
                or bounds.y + bounds.height > (frame.height_px or 0)
                or decision.confidence < 0.90
            ):
                await self._trace(
                    GuideStage.VERIFICATION,
                    GuideTraceOutcome.REJECTED,
                    frame=frame,
                    task=task,
                    reason="pointer_gate_failed",
                )
                return ""
        if (
            decision.decision_kind
            in {
                GuideDecisionKind.INSTRUCT,
                GuideDecisionKind.ANSWER,
            }
            and decision.confidence < 0.85
        ):
            await self._trace(
                GuideStage.VERIFICATION,
                GuideTraceOutcome.REJECTED,
                frame=frame,
                task=task,
                reason="decision_confidence_below_threshold",
            )
            return ""
        spoken = self._kernel.spoken_text(decision.spoken_text)
        if spoken is None:
            await self._trace(
                GuideStage.VERIFICATION,
                GuideTraceOutcome.REJECTED,
                frame=frame,
                task=task,
                reason="spoken_output_gate_failed",
            )
            return ""
        await self._trace(
            GuideStage.VERIFICATION,
            GuideTraceOutcome.SUCCEEDED,
            frame=frame,
            task=task,
            decision_kind=decision.decision_kind,
        )

        def _reduce(updated: GuideTask) -> GuideTask:
            current = updated.current_step
            if current is None:
                return updated
            was_completion_candidate = bool(
                updated.last_verification
                and updated.last_verification.get("completion_candidate") is True
            )
            updated.visual_calls += 1
            updated.last_observation_id = decision.observation.observation_id
            updated.last_frame_id = frame.frame_id
            updated.last_active_app = frame.active_process
            updated.last_verification = {
                "decision_kind": decision.decision_kind,
                "confidence": decision.confidence,
                "frame_id": frame.frame_id,
            }
            if decision.decision_kind == GuideDecisionKind.PAUSE_APP:
                updated.status = GuideTaskStatus.PAUSED_APP
                updated.pause_reason = "target application is not active"
            elif decision.decision_kind == GuideDecisionKind.WAIT:
                updated.status = GuideTaskStatus.WAITING_EXTERNAL
            elif decision.decision_kind == GuideDecisionKind.REPLAN:
                updated.status = GuideTaskStatus.REPLANNING
            elif decision.decision_kind == GuideDecisionKind.CLARIFY:
                updated.status = GuideTaskStatus.CLARIFYING
            elif decision.decision_kind == GuideDecisionKind.VERIFY_CANDIDATE:
                predicates = set(decision.verification_predicates_met)
                required = set(current.verification_predicates)
                sources = list(dict.fromkeys(decision.evidence_sources))
                critical_ok = not current.critical or (
                    decision.confidence >= 0.92 and len(sources) >= 2
                )
                if required.issubset(predicates) and critical_ok:
                    evidence_id = hashlib.sha256(
                        (
                            f"{updated.task_id}|{current.step_id}|{frame.frame_id}|"
                            f"{decision.observation.observation_id}"
                        ).encode()
                    ).hexdigest()[:24]
                    updated.verified_evidence.append(
                        GuideEvidence(
                            evidence_id=evidence_id,
                            step_id=current.step_id,
                            frame_id=frame.frame_id,
                            observation_id=decision.observation.observation_id,
                            predicates=sorted(predicates),
                            sources=sources,
                            summary=decision.observation.summary,
                            recorded_at=datetime.now(UTC),
                        )
                    )
                    current.verified_evidence_refs.append(evidence_id)
                    current.status = GuideStepStatus.VERIFIED
                    next_step = next(
                        (
                            candidate
                            for candidate in updated.steps
                            if candidate.status == GuideStepStatus.PENDING
                            and all(
                                next(
                                    (
                                        dependency
                                        for dependency in updated.steps
                                        if dependency.step_id == dependency_id
                                    ),
                                    None,
                                )
                                is not None
                                and next(
                                    dependency
                                    for dependency in updated.steps
                                    if dependency.step_id == dependency_id
                                ).status
                                == GuideStepStatus.VERIFIED
                                for dependency_id in candidate.dependencies
                            )
                        ),
                        None,
                    )
                    if next_step is not None:
                        next_step.status = GuideStepStatus.ACTIVE
                        updated.current_step_id = next_step.step_id
                        updated.status = GuideTaskStatus.ACTIVE
                    else:
                        completion_ready = self._kernel.completion_ready(updated)
                        if completion_ready and was_completion_candidate:
                            updated.status = GuideTaskStatus.COMPLETED
                            updated.completed_at = datetime.now(UTC)
                        else:
                            updated.status = GuideTaskStatus.COMPLETE_CANDIDATE
                            updated.last_verification = {
                                **(updated.last_verification or {}),
                                "completion_candidate": completion_ready,
                            }
            else:
                if decision.decision_kind == GuideDecisionKind.INSTRUCT and spoken:
                    current.attempt_count += 1
                updated.status = GuideTaskStatus.WAITING_USER if spoken else GuideTaskStatus.ACTIVE
            return updated

        try:
            updated = await self._store.mutate(
                self._user_id,
                task.task_id,
                self._lease_owner,
                input_revision,
                _reduce,
            )
        except (GuideTaskConflictError, GuideTaskLeaseError) as exc:
            logger.info(
                "GuideTelemetry: stale decision rejected",
                {**self._correlation(frame=frame, task=task), "reason": str(exc)},
            )
            await self._trace(
                GuideStage.EXECUTION,
                GuideTraceOutcome.REJECTED,
                frame=frame,
                task=task,
                reason="task_revision_or_lease_changed",
                error=exc,
            )
            return ""
        self._task = updated

        if spoken:
            claim_revision = updated.revision
            claim_step = updated.current_step or step
            instruction_id = hashlib.sha256(
                "|".join(
                    [
                        updated.task_id,
                        str(claim_revision),
                        claim_step.step_id,
                        decision.observation.observation_id,
                        decision.decision_kind,
                        decision.target_control_id or "",
                        decision.expected_next_state,
                    ]
                ).encode()
            ).hexdigest()
            if instruction_id in updated.recent_instruction_ids:
                logger.info(
                    "GuideTelemetry: instruction deduplicated",
                    {
                        **self._correlation(frame=frame, task=updated),
                        "instruction_id": instruction_id,
                    },
                )
                await self._trace(
                    GuideStage.EXECUTION,
                    GuideTraceOutcome.REJECTED,
                    frame=frame,
                    task=updated,
                    reason="instruction_deduplicated",
                )
                await self._publish_task()
                return ""

            def _claim(claimed: GuideTask) -> GuideTask:
                instruction = GuideInstruction(
                    instruction_id=instruction_id,
                    task_revision=claim_revision,
                    step_id=claim_step.step_id,
                    frame_id=frame.frame_id,
                    observation_id=decision.observation.observation_id,
                    spoken_text=spoken,
                    target_control_id=decision.target_control_id,
                    expected_state_delta=decision.expected_next_state,
                    status=GuideInstructionStatus.CLAIMED,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                claimed.pending_instruction = instruction
                claimed.pending_instruction_status = GuideInstructionStatus.CLAIMED
                claimed.recent_instruction_ids.append(instruction_id)
                active_step = claimed.current_step
                if active_step is not None:
                    active_step.last_instruction_id = instruction_id
                return claimed

            try:
                self._task = await self._store.mutate(
                    self._user_id,
                    updated.task_id,
                    self._lease_owner,
                    claim_revision,
                    _claim,
                )
            except (GuideTaskConflictError, GuideTaskLeaseError) as exc:
                await self._trace(
                    GuideStage.EXECUTION,
                    GuideTraceOutcome.REJECTED,
                    frame=frame,
                    task=updated,
                    reason="instruction_claim_conflict",
                    error=exc,
                )
                return ""
            self._last_spoken_instruction_id = instruction_id
            self._speech_started = False
            await self._publish_instruction()
            captured_at_ms = frame.attribute_int("captured_at_ms")
            logger.info(
                "GuideTelemetry: instruction claimed",
                {
                    **self._correlation(frame=frame, task=self._task),
                    "instruction_id": instruction_id,
                    "screen_change_to_instruction_ms": (
                        max(0, int(time.time() * 1000) - captured_at_ms)
                        if captured_at_ms is not None
                        else None
                    ),
                },
            )
            if target is not None:
                await publish_element_point(
                    PointTarget(
                        x=target.bounds.x + target.bounds.width // 2,
                        y=target.bounds.y + target.bounds.height // 2,
                        label=target.label,
                        screen=None,
                    ),
                    frame_id=frame.frame_id,
                    session_id=self._voice_session_id,
                    user_id=self._user_id,
                    coordinate_scale=frame.model_scale,
                )
            await self._trace(
                GuideStage.SPEECH,
                GuideTraceOutcome.STARTED,
                frame=frame,
                task=self._task,
                delivery_status=GuideInstructionStatus.CLAIMED,
            )
        await self._publish_task()
        return spoken

    async def _mutate_status(
        self,
        status: GuideTaskStatus,
        reason: str,
    ) -> None:
        if self._task is None or self._task.status in _TERMINAL_STATES:
            return
        expected = self._task.revision

        def _reduce(task: GuideTask) -> GuideTask:
            task.status = status
            if status in {
                GuideTaskStatus.PAUSED_APP,
                GuideTaskStatus.PAUSED_AWAY,
                GuideTaskStatus.PAUSED_OFFLINE,
            }:
                task.pause_reason = reason
            elif status == GuideTaskStatus.BLOCKED:
                task.blocked_reason = reason
            if (
                status == GuideTaskStatus.PAUSED_OFFLINE
                and task.pending_instruction is not None
                and task.pending_instruction.status
                in {
                    GuideInstructionStatus.CLAIMED,
                    GuideInstructionStatus.SPEECH_STARTED,
                }
            ):
                task.pending_instruction.status = GuideInstructionStatus.DELIVERY_UNKNOWN
                task.pending_instruction.updated_at = datetime.now(UTC)
                task.pending_instruction_status = GuideInstructionStatus.DELIVERY_UNKNOWN
            return task

        try:
            self._task = await self._store.mutate(
                self._user_id,
                self._task.task_id,
                self._lease_owner,
                expected,
                _reduce,
            )
            await self._publish_task()
        except (GuideTaskConflictError, GuideTaskLeaseError):
            self._task = await self._store.load(self._user_id, self._task.task_id)

    async def _mark_instruction(self, status: GuideInstructionStatus) -> None:
        if (
            self._task is None
            or self._task.pending_instruction is None
            or self._task.pending_instruction.instruction_id != self._last_spoken_instruction_id
            or self._task.pending_instruction_status == status
        ):
            return
        expected = self._task.revision

        def _reduce(task: GuideTask) -> GuideTask:
            if (
                task.pending_instruction is not None
                and task.pending_instruction.instruction_id == self._last_spoken_instruction_id
            ):
                task.pending_instruction.status = status
                task.pending_instruction.updated_at = datetime.now(UTC)
                task.pending_instruction_status = status
            return task

        try:
            self._task = await self._store.mutate(
                self._user_id,
                self._task.task_id,
                self._lease_owner,
                expected,
                _reduce,
            )
            await self._publish_instruction()
            await self._publish_task()
        except (GuideTaskConflictError, GuideTaskLeaseError):
            return

    def _start_lease_renewal(self) -> None:
        if self._lease_task is not None:
            return

        async def _renew() -> None:
            while self._active:
                await asyncio.sleep(10)
                task = self._task
                if task is None or task.status in _TERMINAL_STATES:
                    continue
                try:
                    renewed = await self._store.renew_lease(
                        self._user_id,
                        task.task_id,
                        self._lease_owner,
                    )
                    async with self._state_lock:
                        if self._task and self._task.task_id == renewed.task_id:
                            self._task.lease_expires_at = renewed.lease_expires_at
                except Exception as exc:
                    logger.warn(
                        "GuideTelemetry: lease renewal failed",
                        {
                            **self._correlation(task=task),
                            "error_type": type(exc).__name__,
                            "stage": GuideStage.EXECUTION,
                            "outcome": GuideTraceOutcome.FAILED,
                            "reason": "lease_renewal_failed",
                        },
                    )

        self._lease_task = asyncio.create_task(
            _renew(),
            name=f"guide-lease-{self._voice_session_id[:8]}",
        )

    def _start_idle_watchdog(self) -> None:
        if self._idle_task is not None:
            return

        async def _watch() -> None:
            while self._active:
                await asyncio.sleep(15)
                idle_seconds = time.monotonic() - self._last_activity_at
                async with self._state_lock:
                    task = self._task
                    if task is None or task.status in _TERMINAL_STATES:
                        continue
                    if idle_seconds >= 300:
                        await self._mutate_status(
                            GuideTaskStatus.PAUSED_OFFLINE,
                            "no activity for five minutes",
                        )
                        await self._close_session("guide_idle_timeout")
                        return
                    if idle_seconds >= 120 and task.status not in {
                        GuideTaskStatus.WAITING_EXTERNAL,
                        GuideTaskStatus.PAUSED_AWAY,
                    }:
                        await self._mutate_status(
                            GuideTaskStatus.PAUSED_AWAY,
                            "no user or progress activity for two minutes",
                        )

        self._idle_task = asyncio.create_task(
            _watch(),
            name=f"guide-idle-{self._voice_session_id[:8]}",
        )

    async def _close_session(self, reason: str) -> None:
        closer = self._session_closer
        if closer is None:
            await self._session.aclose()
            return
        await closer(reason)

    async def _publish(self, message_type: str, payload: dict[str, Any]) -> None:
        await publish_client_event(
            self._room,
            message_type,
            payload,
            log_message="GuideTelemetry: protocol publish failed",
            log_fields={
                **self._correlation(),
                "message_type": message_type,
                "stage": GuideStage.EXECUTION,
                "outcome": GuideTraceOutcome.FAILED,
                "reason": "protocol_publish_failed",
            },
        )

    async def _publish_task(self) -> None:
        task = self._task
        if task is None:
            return
        step = task.current_step
        await self._publish(
            "guide.task",
            {
                "guide_session_id": self._guide_session_id,
                "task_id": task.task_id,
                "revision": task.revision,
                "status": task.status,
                "current_step_id": task.current_step_id,
                "current_step_title": step.title if step else "",
                "resumable": task.resumable,
                "completion": task.status == GuideTaskStatus.COMPLETED,
            },
        )

    async def _publish_instruction(self) -> None:
        task = self._task
        instruction = task.pending_instruction if task else None
        if task is None or instruction is None:
            return
        await self._publish(
            "guide.instruction",
            {
                "guide_session_id": self._guide_session_id,
                "task_id": task.task_id,
                "revision": task.revision,
                "step_id": instruction.step_id,
                "instruction_id": instruction.instruction_id,
                "frame_id": instruction.frame_id,
                "delivery_status": instruction.status,
                "done": task.status == GuideTaskStatus.COMPLETED,
            },
        )

    def _trace_for(
        self,
        *,
        frame: ScreenFrame | None = None,
        task: GuideTask | None = None,
    ) -> GuideTraceContext:
        task = task or self._task
        attributes = frame.attributes if frame else {}
        trace_id = attributes.get("trace_id") or self._current_trace.trace_id
        event_id = attributes.get("event_id") or self._current_trace.event_id
        parent_event_id = (
            attributes.get("parent_event_id")
            or self._current_trace.parent_event_id
        )
        step = task.current_step if task else None
        return GuideTraceContext(
            trace_id=trace_id,
            event_id=event_id,
            parent_event_id=parent_event_id,
            fields={
                "user_id": self._user_id,
                "voice_session_id": self._voice_session_id,
                "guide_session_id": self._guide_session_id,
                "task_id": task.task_id if task else None,
                "task_revision": task.revision if task else None,
                "run_id": self._run_id,
                "frame_id": frame.frame_id if frame else None,
                "observation_id": task.last_observation_id if task else None,
                "step_id": step.step_id if step else None,
                "instruction_id": (
                    task.pending_instruction.instruction_id
                    if task and task.pending_instruction
                    else None
                ),
                "speech_epoch": self._speech_epoch,
                "task_profile_id": (
                    task.task_profile_id if task else self._profile.profile_id
                ),
            },
        )

    async def _trace(
        self,
        stage: GuideStage,
        outcome: GuideTraceOutcome,
        *,
        frame: ScreenFrame | None = None,
        task: GuideTask | None = None,
        reason: str | None = None,
        error: Exception | None = None,
        **extra: object,
    ) -> None:
        trace = self._trace_for(frame=frame, task=task)
        self._current_trace = trace
        payload = trace.payload(
            stage=stage,
            outcome=outcome,
            reason=reason,
            error_type=type(error).__name__ if error else None,
            **extra,
        )
        if outcome == GuideTraceOutcome.FAILED:
            logger.warn("GuideTrace", payload)
            await self._publish(
                "guide.failure",
                {
                    "guide_session_id": self._guide_session_id,
                    "trace_id": trace.trace_id,
                    "event_id": trace.event_id,
                    "task_id": self._task.task_id if self._task else None,
                    "task_revision": self._task.revision if self._task else None,
                    "stage": stage,
                    "reason": reason or "unknown",
                    "error_type": type(error).__name__ if error else None,
                },
            )
        else:
            logger.info("GuideTrace", payload)

    def _correlation(
        self,
        *,
        frame: ScreenFrame | None = None,
        task: GuideTask | None = None,
    ) -> dict[str, Any]:
        trace = self._trace_for(frame=frame, task=task)
        return trace.fields | {
            "trace_id": trace.trace_id,
            "event_id": trace.event_id,
            "parent_event_id": trace.parent_event_id,
        }
